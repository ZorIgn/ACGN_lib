"""Reviewed, owner-scoped merging of duplicate personal records."""

import json
import secrets

from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError, models, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from app.library import KINDS, STATES, model_for
from app.models import LibraryFolder, LibraryLink, LibrarySeriesMember

SESSION_KEY = "library_merge_preview"
TOKEN_SALT = "library-record-merge"
TOKEN_SECONDS = 900
CHOICE_LABELS = {
    "score": "评分",
    "status": "状态",
    "position": "阅读 / 观看位置",
    "url": "观看 / 阅读链接",
}


class MergeForm(forms.Form):
    source = forms.ChoiceField(label="待合并记录")
    target = forms.ChoiceField(label="保留记录")

    def __init__(self, *args, records, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [
            (
                key,
                f"{KINDS[row.item.media_type]} · {row.item.title} · {row.item.source} · #{row.pk}",
            )
            for key, row in records.items()
        ]
        for key in ["source", "target"]:
            self.fields[key].choices = [("", "请选择作品记录"), *choices]
        for key, label in CHOICE_LABELS.items():
            self.fields[key] = forms.ChoiceField(
                label=f"{label}取值",
                required=False,
                choices=[
                    ("", "保留记录优先，空值时取待合并记录"),
                    ("target", "使用保留记录（包括空值）"),
                    ("source", "使用待合并记录（包括空值）"),
                ],
            )


def owned_records(user):
    return {
        f"{kind}:{row.pk}": row
        for kind in KINDS
        for row in model_for(kind)
        .objects.filter(user=user)
        .select_related("item")
        .order_by("item__title", "pk")
    }


def _validate_pair(source, target):
    if (
        source.item.media_type != target.item.media_type
        or source.__class__ is not target.__class__
    ):
        raise ValidationError("只能合并同一类型的作品记录。")
    if source.pk == target.pk:
        raise ValidationError("请选择两条不同的记录。")
    if source.item.media_type == "tv" and (
        source.seasons.exists() or target.seasons.exists()
    ):
        raise ValidationError("含分季或分集记录的剧集不能合并，请使用系列与版本关联。")
    if source.item.source == "steam":
        if target.item.source != "steam":
            raise ValidationError("请将 Steam 作品选为保留记录，以保留后续同步身份。")
        if source.item.media_id != target.item.media_id:
            raise ValidationError("不同 Steam 游戏不能合并，请使用版本关联。")


def _snapshot(user, source, target):
    item_ids = {source.item_id, target.item_id}
    record_data = []
    for row in [source, target]:
        record_data.append(
            {
                "fields": {
                    field.attname: getattr(row, field.attname)
                    for field in row._meta.concrete_fields
                },
                "item": {
                    field.attname: getattr(row.item, field.attname)
                    for field in row.item._meta.concrete_fields
                },
                "history": list(row.history.order_by("history_id").values()),
            }
        )
    payload = {
        "records": record_data,
        "links": list(
            LibraryLink.objects.filter(user=user, item_id__in=item_ids)
            .order_by("pk")
            .values()
        ),
        "folders": list(
            LibraryFolder.objects.filter(user=user, items__in=item_ids)
            .order_by("pk", "items")
            .values("id", "name", "items")
        ),
        "series": list(
            LibrarySeriesMember.objects.filter(series__user=user, item_id__in=item_ids)
            .order_by("pk")
            .values(
                "id",
                "series_id",
                "series__name",
                "series__kind",
                "item_id",
                "label",
                "sort_order",
            )
        ),
        "related_records": {
            kind: list(
                model_for(kind)
                .objects.filter(user=user, item_id__in=item_ids)
                .order_by("pk")
                .values_list("pk", "item_id")
            )
            for kind in KINDS
        },
    }
    return json.loads(json.dumps(payload, cls=DjangoJSONEncoder))


def _plan(user, source, target, choices):
    links = {
        row.item_id: row
        for row in LibraryLink.objects.filter(
            user=user, item_id__in=[source.item_id, target.item_id]
        )
    }
    result = {}
    comparison = []
    for key, label in CHOICE_LABELS.items():
        if key in {"position", "url"}:
            source_value = getattr(links.get(source.item_id), key, "")
            target_value = getattr(links.get(target.item_id), key, "")
        else:
            source_value, target_value = getattr(source, key), getattr(target, key)
        choice = choices.get(key)
        value = (
            source_value
            if choice == "source" or not choice and target_value in (None, "")
            else target_value
        )
        result[key] = value

        def display(current, field=key):
            if field == "status":
                return STATES.get(current, "未设置")
            return "未设置" if current in (None, "") else str(current)

        comparison.append(
            {
                "label": label,
                "source": display(source_value),
                "target": display(target_value),
                "result": display(value),
            }
        )
    paragraphs = []
    for note in [target.notes, source.notes]:
        for paragraph in note.split("\n\n"):
            text = paragraph.strip()
            if text and text not in paragraphs:
                paragraphs.append(text)
    result["notes"] = "\n\n".join(paragraphs)
    if target.item.media_type != "tv":
        result["progress"] = max(target.progress, source.progress)
        for field, aggregate in [
            ("start_date", min),
            ("end_date", max),
            ("progressed_at", max),
        ]:
            values = [
                value
                for value in [getattr(source, field), getattr(target, field)]
                if value is not None
            ]
            result[field] = aggregate(values) if values else None
    folders = list(
        LibraryFolder.objects.filter(
            user=user, items__in=[source.item_id, target.item_id]
        ).distinct()
    )
    series = {}
    for item_id in [source.item_id, target.item_id]:
        for member in LibrarySeriesMember.objects.filter(
            series__user=user, item_id=item_id
        ).select_related("series"):
            conflict = member.series_id in series and item_id != source.item_id
            series[member.series_id] = {
                "name": member.series.name,
                "label": member.label,
                "sort_order": member.sort_order,
                "conflict": conflict,
            }
    return {
        "result": result,
        "comparison": comparison,
        "folders": folders,
        "series": list(series.values()),
        "source": source,
        "target": target,
    }


def _apply(user, source, target, plan):
    values = plan["result"]
    fields = [key for key in values if key not in {"position", "url", "progressed_at"}]
    for field in fields:
        setattr(target, field, values[field])
    source.history.update(id=target.pk)
    models.Model.save(target, update_fields=fields)
    if "progressed_at" in values:
        # MonitorField would stamp the merge time instead of the latest original progress.
        target.__class__.objects.filter(pk=target.pk).update(
            progressed_at=values["progressed_at"]
        )
    if values["position"] or values["url"] or target.item.source == "steam":
        LibraryLink.objects.update_or_create(
            user=user,
            item=target.item,
            defaults={"position": values["position"], "url": values["url"]},
        )
    else:
        LibraryLink.objects.filter(user=user, item=target.item).delete()
    for folder in plan["folders"]:
        folder.items.add(target.item)
    for member in LibrarySeriesMember.objects.filter(
        series__user=user, item=source.item
    ):
        LibrarySeriesMember.objects.get_or_create(
            series=member.series,
            item=target.item,
            defaults={"label": member.label, "sort_order": member.sort_order},
        )
    source.delete()
    if source.item_id != target.item_id and not any(
        model_for(kind).objects.filter(user=user, item_id=source.item_id).exists()
        for kind in KINDS
    ):
        LibraryLink.objects.filter(user=user, item_id=source.item_id).delete()
        for folder in LibraryFolder.objects.filter(user=user, items=source.item):
            folder.items.remove(source.item)
        LibrarySeriesMember.objects.filter(
            series__user=user, item_id=source.item_id
        ).delete()


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def merge(request):
    records = owned_records(request.user)
    initial_source = request.GET.get("source", "")
    if initial_source and initial_source not in records:
        raise Http404
    action = request.POST.get("action") if request.method == "POST" else None
    form = MergeForm(
        request.POST if action == "preview" else None,
        records=records,
        initial={"source": initial_source},
    )
    preview, token, error = None, "", ""
    try:
        if action == "preview" and form.is_valid():
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=request.user.pk)
                rows = [records[form.cleaned_data[key]] for key in ["source", "target"]]
                source, target = [
                    row.__class__.objects.select_for_update()
                    .select_related("item")
                    .get(pk=row.pk, user=request.user)
                    for row in rows
                ]
                _validate_pair(source, target)
                choices = {key: form.cleaned_data[key] for key in CHOICE_LABELS}
                preview = _plan(request.user, source, target, choices)
                token = signing.dumps(
                    {"owner": request.user.pk, "nonce": secrets.token_urlsafe(24)},
                    salt=TOKEN_SALT,
                )
                request.session[SESSION_KEY] = {
                    "token": token,
                    "source": form.cleaned_data["source"],
                    "target": form.cleaned_data["target"],
                    "choices": choices,
                    "snapshot": _snapshot(request.user, source, target),
                }
        elif action == "confirm":
            pending = request.session.get(SESSION_KEY, {})
            token = request.POST.get("token", "")
            try:
                signed = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_SECONDS)
                valid = signed.get("owner") == request.user.pk and token == pending.get(
                    "token"
                )
            except (signing.BadSignature, TypeError, AttributeError):
                valid = False
            if not valid:
                raise ValidationError("预览已失效，请重新选择记录并预览。")
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=request.user.pk)
                try:
                    rows = []
                    for key in ["source", "target"]:
                        kind, record_id = pending[key].split(":")
                        rows.append(
                            model_for(kind)
                            .objects.select_for_update()
                            .select_related("item")
                            .get(user=request.user, pk=record_id)
                        )
                    source, target = rows
                except (KeyError, ValueError, ObjectDoesNotExist) as exc:
                    raise ValidationError("记录已变化，请重新预览后确认。") from exc
                _validate_pair(source, target)
                if _snapshot(request.user, source, target) != pending["snapshot"]:
                    raise ValidationError(
                        "记录、分类或编辑历史已变化，请重新预览后确认。"
                    )
                _apply(
                    request.user,
                    source,
                    target,
                    _plan(request.user, source, target, pending["choices"]),
                )
            request.session.pop(SESSION_KEY, None)
            messages.success(request, "重复记录已合并，编辑历史与分类已保留。")
            return redirect(
                "library_detail", kind=target.item.media_type, record_id=target.pk
            )
        elif action not in (None, "preview"):
            raise ValidationError("请选择有效操作。")
    except ObjectDoesNotExist:
        error = "记录已变化，请重新预览后确认。"
    except ValidationError as exc:
        error = " ".join(exc.messages)
    except DatabaseError:
        error = "暂时无法合并，请稍后重新预览。记录尚未更改。"
    return render(
        request,
        "app/library/merge.html",
        {"form": form, "preview": preview, "token": token, "error": error},
        status=400 if error or form.errors else 200,
    )
