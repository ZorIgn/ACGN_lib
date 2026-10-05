"""Personal relationships between independently rated library works."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from app.library import KINDS, STATES, model_for
from app.models import LibrarySeries, LibrarySeriesMember


class SeriesForm(forms.Form):
    name = forms.CharField(label="名称", max_length=80)
    kind = forms.ChoiceField(label="关联类型", choices=LibrarySeries.KINDS)


class MemberForm(forms.Form):
    work = forms.ChoiceField(label="书架中的作品")
    label = forms.CharField(label="部次 / 版本说明", max_length=80, required=False)
    sort_order = forms.IntegerField(
        label="排序序号", min_value=0, max_value=1000000, initial=0
    )

    def __init__(self, *args, records, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["work"].choices = [
            (
                key,
                f"{KINDS[row.item.media_type]} · {row.item.title} · {row.item.source} · #{row.pk}",
            )
            for key, row in records.items()
        ]


def owned_records(user):
    records = {}
    for kind in KINDS:
        for record in (
            model_for(kind)
            .objects.filter(user=user)
            .select_related("item")
            .order_by("-created_at", "-pk")
        ):
            records[f"{kind}:{record.pk}"] = record
    return records


def identifier(value):
    try:
        key = int(value)
    except (ValueError, TypeError) as error:
        raise Http404 from error
    if not 1 <= key <= 2**63 - 1:
        raise Http404
    return key


@login_required
@require_http_methods(["GET", "POST"])
def manage(request):
    records = owned_records(request.user)
    groups = LibrarySeries.objects.filter(user=request.user)
    group = None
    group_id = (
        request.POST.get("group")
        if request.method == "POST"
        else request.GET.get("group")
    )
    if group_id:
        group = get_object_or_404(groups, pk=identifier(group_id))
    chosen_work = request.GET.get("work", "")
    if chosen_work and chosen_work not in records:
        raise Http404
    action = request.POST.get("action") if request.method == "POST" else None
    series_form = SeriesForm(
        request.POST if action in {"create", "edit"} else None,
        initial={"name": group.name, "kind": group.kind} if group else None,
    )
    member_form = MemberForm(
        request.POST if action == "member" else None,
        records=records,
        initial={"work": chosen_work},
    )
    error = ""
    if action in {"create", "edit"} and series_form.is_valid():
        if action == "edit" and group is None:
            raise Http404
        try:
            with transaction.atomic():
                if action == "create":
                    group = LibrarySeries.objects.create(
                        user=request.user, **series_form.cleaned_data
                    )
                else:
                    group.name = series_form.cleaned_data["name"]
                    group.kind = series_form.cleaned_data["kind"]
                    group.save(update_fields=["name", "kind"])
        except IntegrityError:
            series_form.add_error("name", "你已创建同名关联，请选择它或使用其他名称。")
        else:
            return redirect(
                f"/library/series/?group={group.pk}"
                + (f"&work={chosen_work}" if chosen_work else "")
            )
    elif action == "member" and group and member_form.is_valid():
        row = records[member_form.cleaned_data["work"]]
        LibrarySeriesMember.objects.update_or_create(
            series=group,
            item=row.item,
            defaults={
                key: member_form.cleaned_data[key] for key in ["label", "sort_order"]
            },
        )
        messages.success(request, "已保存作品关联，各部作品仍可分别评分。")
        return redirect(f"/library/series/?group={group.pk}")
    elif action == "remove" and group:
        get_object_or_404(
            LibrarySeriesMember, pk=identifier(request.POST.get("member")), series=group
        ).delete()
        return redirect(f"/library/series/?group={group.pk}")
    elif action == "delete" and group:
        group.delete()
        messages.success(request, "已删除关联组，书架作品与评分仍保留。")
        return redirect("library_series")
    elif action and (
        action not in {"create", "edit", "member"}
        or action in {"edit", "member"}
        and not group
    ):
        error = "请选择有效的关联组和操作。"
    by_item = {}
    for key, record in records.items():
        by_item.setdefault(record.item_id, (key, record))
    members = []
    if group:
        for member in group.members.select_related("item"):
            if member.item_id in by_item:
                key, record = by_item[member.item_id]
                members.append(
                    {
                        "member": member,
                        "record": record,
                        "key": key,
                        "kind": record.item.media_type,
                        "status": STATES.get(record.status, "未设置"),
                    }
                )
    return render(
        request,
        "app/library/series.html",
        {
            "groups": groups,
            "group": group,
            "series_form": series_form,
            "member_form": member_form,
            "members": members,
            "chosen_work": chosen_work,
            "error": error,
        },
        status=400 if error or series_form.errors or member_form.errors else 200,
    )
