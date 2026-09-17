"""Private bookshelf, reviewed imports and personal records."""

import json
import re
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from django import forms
from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.core.validators import URLValidator
from django.db import models, transaction
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from app.models import Item, LibraryImportDraft, LibraryLink, Sources
from app.providers import bangumi, services, webnovel

KINDS = {
    "book": "小说",
    "manga": "漫画",
    "anime": "动画",
    "tv": "剧集",
    "movie": "电影",
    "game": "游戏",
}
STATES = {
    "Planning": "想看 / 想玩",
    "In progress": "进行中",
    "Completed": "已完成",
    "Paused": "暂停",
    "Dropped": "已放弃",
}
BATCH_SIZE = 20


class StarRatingInput(forms.NumberInput):
    """Render five stars with one point per half-star."""

    template_name = "app/library/widgets/rating.html"

    def get_context(self, name, value, attrs):
        """Pair the two clickable scores represented by each star."""
        context = super().get_context(name, value, attrs)
        context["star_scores"] = [
            {"half": index * 2 - 1, "whole": index * 2} for index in range(1, 6)
        ]
        return context


class PersonalRecordForm(forms.Form):
    """Validate only fields the owner edits."""

    status = forms.ChoiceField(
        label="状态", required=False, choices=[("", "未设置"), *STATES.items()]
    )
    score = forms.DecimalField(
        label="我的评分",
        required=False,
        min_value=0,
        max_value=10,
        max_digits=3,
        decimal_places=1,
        widget=StarRatingInput(),
    )
    notes = forms.CharField(
        label="我的记录",
        required=False,
        max_length=10000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    viewing_url = forms.URLField(
        label="观看 / 阅读链接", required=False, max_length=2000, assume_scheme="https"
    )
    status_manual = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def clean(self):
        """Respect an explicit status while defaulting a new rating to completed."""
        data = super().clean()
        if (
            data.get("score") is not None
            and not data.get("status")
            and not data.get("status_manual")
        ):
            data["status"] = "Completed"
        return data

    def clean_viewing_url(self):
        """Keep saved links limited to browser navigation."""
        url = self.cleaned_data["viewing_url"]
        if url:
            URLValidator(schemes=["http", "https"])(url)
        return url


class CaptureForm(forms.Form):
    """Accept names or supported catalog URLs."""

    media_type = forms.ChoiceField(label="作品类型", choices=KINDS.items())
    entries = forms.CharField(
        label="作品名称或链接",
        max_length=10000,
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "placeholder": "每行一部作品\n道诡异仙\n小说 | 仙逆 | 很喜欢\n游戏 | 哈迪斯",
            }
        ),
    )

    def clean_entries(self):
        """Bound the lookup work and remove repeated input lines."""
        lines = list(
            dict.fromkeys(
                line.strip()
                for line in self.cleaned_data["entries"].splitlines()
                if line.strip()
            )
        )
        if len(lines) > 500:
            raise forms.ValidationError("每份清单最多 500 部作品。")
        rows = []
        labels = {label: kind for kind, label in KINDS.items()}
        for line in lines:
            parts = re.split(r"\s*[|｜\t]\s*", line, maxsplit=2)
            kind = self.cleaned_data.get("media_type", "book")
            notes = ""
            if len(parts) > 1 and parts[0] in labels:
                kind, line = labels[parts[0]], parts[1]
                notes = parts[2] if len(parts) == 3 else ""
            if not line:
                raise forms.ValidationError("请填写作品名称。")
            rows.append({"input": line, "media_type": kind, "notes": notes})
        return rows


def model_for(kind):
    """Resolve one of the supported record models."""
    if kind not in KINDS:
        raise Http404
    return apps.get_model("app", kind)


def page_context(**kwargs):
    """Share the small navigation vocabulary."""
    return {"kinds": KINDS, "states": STATES, **kwargs}


@require_GET
def shelf(request):
    """Show the owner's media without contacting metadata providers."""
    kind = request.GET.get("type", "")
    status = request.GET.get("status", "")
    query = request.GET.get("q", "").strip()
    items = []
    for key in KINDS:
        if kind and kind != key:
            continue
        records = (
            model_for(key).objects.filter(user=request.user).select_related("item")
        )
        if status in STATES:
            records = records.filter(status=status)
        if query:
            records = records.filter(item__title__icontains=query)
        for record in records:
            items.append(
                {
                    "record": record,
                    "kind": key,
                    "kind_label": KINDS[key],
                    "status_label": STATES.get(record.status, "未设置"),
                    "hours": record.progress / 60 if key == "game" else None,
                }
            )
    items.sort(key=lambda entry: entry["record"].created_at, reverse=True)
    return render(
        request,
        "app/library/shelf.html",
        page_context(
            page=Paginator(items, 36).get_page(request.GET.get("page")),
            kind=kind,
            status=status,
            query=query,
            draft_count=LibraryImportDraft.objects.filter(user=request.user).count(),
        ),
    )


def lookup(entry):
    """Resolve an input without fetching arbitrary third-party URLs."""
    text = entry["input"].strip()
    kind = entry["media_type"]
    viewing_url = ""
    candidates = []
    error = ""
    try:
        parsed = urlparse(text)
        if parsed.scheme in {"http", "https"}:
            host = (parsed.hostname or "").lower()
            bgm_match = re.fullmatch(r"/subject/(\d+)/?", parsed.path)
            tmdb_match = re.match(r"/(movie|tv)/(\d+)(?:[-/]|$)", parsed.path)
            if host in {"bgm.tv", "bangumi.tv", "chii.in"} and bgm_match:
                data = bangumi.subject(bgm_match[1])
                candidate = bangumi.result(data)
                if candidate["media_type"] in KINDS:
                    candidates = [candidate]
            elif host in {"themoviedb.org", "www.themoviedb.org"} and tmdb_match:
                candidates = [
                    services.get_media_metadata(
                        tmdb_match[1], tmdb_match[2], Sources.TMDB
                    )
                ]
            else:
                viewing_url = text
                error = "请补充作品名称；这个链接会保存在你的观看 / 阅读链接中。"
        else:
            source = Sources.TMDB if kind == "movie" else Sources.BANGUMI
            try:
                candidates = services.search(kind, text, 1, source)["results"]
            except services.ProviderAPIError:
                if kind != "book":
                    raise
            candidates = [
                candidate for candidate in candidates if title_matches(text, candidate)
            ]
            if kind == "book" and not candidates:
                candidates = [
                    candidate
                    for candidate in webnovel.search(text)
                    if title_matches(text, candidate)
                ]
            candidates = candidates[:5]
    except services.ProviderAPIError:
        error = "作品资料暂时无法获取，可以稍后重试，也可以按名称创建。"
    choices = [
        {**candidate, "kind_label": KINDS[candidate["media_type"]]}
        for candidate in candidates
    ]
    manual = {
        "source": Sources.MANUAL.value,
        "media_type": kind,
        "title": text if not viewing_url else "",
        "image": settings.IMG_NONE,
    }
    return {
        **entry,
        "choices": choices,
        "error": error,
        "viewing_url": viewing_url,
        "manual": manual,
        "manual_title": manual["title"],
    }


def title_matches(query, candidate):
    """Require the searched title fragment, not an unrelated fuzzy result."""

    def normalize(value):
        return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())

    needle = normalize(query)
    return bool(needle) and any(
        needle in normalize(candidate.get(key) or "")
        for key in ("title", "original_title")
    )


def review_rows(entries):
    """Look up a bounded batch with limited network concurrency."""
    with ThreadPoolExecutor(max_workers=3) as executor:
        return list(executor.map(lookup, entries))


def seed_draft(user):
    """Load the optional private starter list once for its owner."""
    path = settings.BASE_DIR / "db" / "initial-library.json"
    draft_id = uuid.uuid5(uuid.NAMESPACE_URL, f"acglib/starter/{user.pk}")
    if path.exists() and not LibraryImportDraft.objects.filter(pk=draft_id).exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = [
            {key: entry.get(key, "") for key in ("input", "media_type", "notes")}
            for entry in data
        ]
        LibraryImportDraft.objects.get_or_create(
            pk=draft_id,
            defaults={"user": user, "title": "对话整理的作品", "entries": entries},
        )


def draft_url(draft, page=1):
    return f"/library/add/?draft={draft.pk}&page={page}"


def selected_count(draft):
    return sum(len(row.get("selected", [])) for row in draft.entries)


def resolve_page(draft, page_number):
    """Fetch catalog data only for unresolved entries on the requested page."""
    page = Paginator(draft.entries, BATCH_SIZE).get_page(page_number)
    indices = range(page.start_index() - 1, page.end_index())
    pending = [index for index in indices if "choices" not in draft.entries[index]]
    if pending:
        found = review_rows([draft.entries[index] for index in pending])
        for index, row in zip(pending, found, strict=True):
            draft.entries[index] = row
        draft.save(update_fields=["entries", "updated_at"])
    return Paginator(draft.entries, BATCH_SIZE).get_page(page_number)


def render_draft(request, draft, page_number=1, errors=None):
    page = resolve_page(draft, page_number)
    rows = []
    for index in range(page.start_index() - 1, page.end_index()):
        row = dict(draft.entries[index])
        row["index"] = index
        row["options"] = [dict(choice) for choice in row["choices"]] + [
            {"title": row["manual_title"], "manual": True}
        ]
        for option_index, option in enumerate(row["options"]):
            option["index"] = option_index
            option["selected"] = option_index in row.get("selected", [])
            data = row.get("personal", {}).get(
                str(option_index),
                {
                    "notes": row.get("notes", ""),
                    "status": "",
                    "score": "",
                    "viewing_url": row.get("viewing_url", ""),
                    "status_manual": "",
                },
            )
            prefix = f"{index}-{option_index}"
            option["personal"] = PersonalRecordForm(
                {f"{prefix}-{key}": value for key, value in data.items()}
                if errors
                else None,
                prefix=prefix,
                initial=data,
            )
        rows.append(row)
    return render(
        request,
        "app/library/capture.html",
        page_context(
            form=CaptureForm(),
            draft=draft,
            draft_page=page,
            rows=rows,
            draft_selected_count=selected_count(draft),
            errors=errors,
        ),
        status=400 if errors else 200,
    )


def save_draft_page(draft, post):
    """Merge one page without touching saved choices on other pages."""
    page = Paginator(draft.entries, BATCH_SIZE).get_page(post.get("page", 1))
    for index in range(page.start_index() - 1, page.end_index()):
        row = draft.entries[index]
        if "choices" not in row:
            continue
        allowed = range(len(row["choices"]) + 1)
        picked = post.getlist(f"choice_{index}")
        if any(not value.isdigit() or int(value) not in allowed for value in picked):
            raise ValueError("所选作品不在这组候选中。")
        row["selected"] = list(dict.fromkeys(int(value) for value in picked))
        row["manual_title"] = post.get(f"manual_title_{index}", row["manual_title"])[
            :500
        ]
        personal = row.setdefault("personal", {})
        for option in allowed:
            values = personal.setdefault(
                str(option),
                {
                    "notes": row.get("notes", ""),
                    "status": "",
                    "score": "",
                    "viewing_url": row.get("viewing_url", ""),
                    "status_manual": "",
                },
            )
            for field in PersonalRecordForm.base_fields:
                name = f"{index}-{option}-{field}"
                if name in post:
                    values[field] = post[name][:10000]
    draft.save(update_fields=["entries", "updated_at"])


@require_http_methods(["GET", "POST"])
def capture(request):
    """Create, resume, reuse, save, and import an owner's draft."""
    seed_draft(request.user)
    draft_id = (
        request.POST.get("draft_id")
        if request.method == "POST"
        else request.GET.get("draft")
    )
    if draft_id:
        try:
            draft_id = uuid.UUID(draft_id)
        except ValueError as error:
            raise Http404 from error
        draft = get_object_or_404(LibraryImportDraft, pk=draft_id, user=request.user)
        if request.method == "POST":
            try:
                with transaction.atomic():
                    draft = LibraryImportDraft.objects.select_for_update().get(
                        pk=draft.pk
                    )
                    save_draft_page(draft, request.POST)
            except ValueError as error:
                return JsonResponse({"ok": False, "error": str(error)}, status=400)
            if request.POST.get("action") == "draft":
                return JsonResponse(
                    {"ok": True, "selected_count": selected_count(draft)}
                )
            if request.POST.get("target_page"):
                return redirect(draft_url(draft, request.POST["target_page"]))
            return import_draft(request, draft)
        return render_draft(request, draft, request.GET.get("page", 1))
    initial = None
    if request.GET.get("reuse"):
        try:
            reuse_id = uuid.UUID(request.GET["reuse"])
        except ValueError as error:
            raise Http404 from error
        draft = get_object_or_404(LibraryImportDraft, pk=reuse_id, user=request.user)
        initial = {
            "media_type": "book",
            "entries": "\n".join(
                f"{KINDS[row['media_type']]} | {row['input']} | {row.get('notes', '')}"
                for row in draft.entries
            ),
        }
    form = CaptureForm(
        request.POST if request.method == "POST" else None, initial=initial
    )
    if request.method == "POST" and form.is_valid():
        entries = form.cleaned_data["entries"]
        draft = LibraryImportDraft.objects.create(
            user=request.user,
            title=f"{entries[0]['input'][:150]}等 {len(entries)} 部作品",
            entries=entries,
        )
        return redirect(draft_url(draft))
    return render(
        request,
        "app/library/capture.html",
        page_context(
            form=form,
            drafts=LibraryImportDraft.objects.filter(user=request.user).order_by(
                "-updated_at"
            ),
        ),
    )


def import_draft(request, draft):
    """Validate the full selection before saving the batch atomically."""
    records = []
    errors = []
    error_page = 1
    for index, row in enumerate(draft.entries):
        for option_index in row.get("selected", []):
            candidate = dict(
                row["choices"][option_index]
                if option_index < len(row["choices"])
                else row["manual"]
            )
            form = PersonalRecordForm(
                row.get("personal", {}).get(str(option_index), {})
            )
            if not form.is_valid():
                if not errors:
                    error_page = index // BATCH_SIZE + 1
                errors.append(
                    f"{candidate.get('title') or '自定义作品'}：{form.errors.as_text()}"
                )
                continue
            if candidate["source"] == Sources.MANUAL:
                candidate["title"] = row["manual_title"].strip()[:500]
                if not candidate["title"]:
                    if not errors:
                        error_page = index // BATCH_SIZE + 1
                    errors.append("自定义作品需要填写名称。")
                    continue
                candidate["media_id"] = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{request.user.pk}/{candidate['media_type']}/{candidate['title']}",
                    )
                )
            records.append((candidate, form.cleaned_data))
    if not records and not errors:
        errors.append("请选择要加入书架的作品。")
    if errors:
        return render_draft(request, draft, error_page, errors=errors)
    created = 0
    with transaction.atomic():
        for candidate, personal in records:
            item, _ = Item.objects.get_or_create(
                source=candidate["source"],
                media_type=candidate["media_type"],
                media_id=candidate["media_id"],
                defaults={"title": candidate["title"], "image": candidate["image"]},
            )
            model = model_for(candidate["media_type"])
            if model.objects.filter(user=request.user, item=item).exists():
                continue
            record = model(
                user=request.user,
                item=item,
                status=personal["status"],
                score=personal["score"],
                notes=personal["notes"],
            )
            # A status selection does not establish dates or episode completion.
            models.Model.save(record)
            if personal["viewing_url"]:
                LibraryLink.objects.update_or_create(
                    user=request.user,
                    item=item,
                    defaults={"url": personal["viewing_url"]},
                )
            created += 1
        draft.imported_at = timezone.now()
        draft.save(update_fields=["imported_at", "updated_at"])
    messages.success(request, f"已加入 {created} 部作品；已有条目保持原样。")
    return redirect("library")


@require_http_methods(["GET", "POST"])
def detail(request, kind, record_id):
    """Read and edit one record owned by the current user."""
    record = get_object_or_404(
        model_for(kind).objects.select_related("item"), pk=record_id, user=request.user
    )
    link = LibraryLink.objects.filter(user=request.user, item=record.item).first()
    form = PersonalRecordForm(
        request.POST if request.method == "POST" else None,
        initial={
            "score": record.score,
            "status": record.status,
            "notes": record.notes,
            "viewing_url": link.url if link else "",
            "status_manual": record.score is not None and record.status == "",
        },
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            for key in ("score", "status", "notes"):
                setattr(record, key, form.cleaned_data[key])
            models.Model.save(record, update_fields=["score", "status", "notes"])
            if form.cleaned_data["viewing_url"]:
                LibraryLink.objects.update_or_create(
                    user=request.user,
                    item=record.item,
                    defaults={"url": form.cleaned_data["viewing_url"]},
                )
            else:
                LibraryLink.objects.filter(user=request.user, item=record.item).delete()
        messages.success(request, "已保存。")
        return redirect("library_detail", kind=kind, record_id=record.pk)
    metadata = {}
    try:
        metadata = services.get_media_metadata(
            kind, record.item.media_id, record.item.source
        )
    except services.ProviderAPIError:
        messages.info(request, "作品资料暂时无法刷新，你的记录仍可编辑。")
    return render(
        request,
        "app/library/detail.html",
        page_context(
            record=record,
            kind=kind,
            kind_label=KINDS[kind],
            form=form,
            metadata=metadata,
            playtime_hours=record.progress / 60 if kind == "game" else None,
            viewing_link=link.url if link else "",
        ),
    )


@require_GET
def source(request):
    """Offer the corresponding application source distributed with this build."""
    archive = settings.BASE_DIR.parent / "source.zip"
    if not archive.exists():
        raise Http404
    return FileResponse(
        archive.open("rb"), as_attachment=True, filename="ACGLib-source.zip"
    )
