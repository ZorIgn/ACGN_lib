"""Preview and import public Bangumi collections into the owner's library."""

import json
import secrets
import time
from urllib.parse import quote

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from app import library_transfer
from app.library import KINDS, STATES
from app.providers import bangumi, services

SESSION_KEY = "library_bangumi_preview"
PREVIEW_SECONDS = 30 * 60
PAGE_SIZE = 50
MAX_COLLECTIONS = 10000
STATUS_MAP = {
    1: "Planning",
    2: "Completed",
    3: "In progress",
    4: "Paused",
    5: "Dropped",
}


class UsernameForm(forms.Form):
    """Accept a Bangumi profile identifier, never an authentication credential."""

    username = forms.RegexField(
        regex=r"\A[A-Za-z0-9_]{1,64}\Z",
        max_length=64,
        label="Bangumi 用户名 / ID",
        error_messages={"invalid": "请填写个人主页 /user/ 后的用户名或数字 ID。"},
        widget=forms.TextInput(attrs={"placeholder": "个人主页 /user/ 后的内容"}),
    )


def _integer(value, *, minimum=0, maximum=2147483647):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError("Bangumi 返回的收藏数据不完整，请稍后重新获取。")
    return value


def _work(entry):
    """Map one public collection; zero on Bangumi means an absent rating."""
    if not isinstance(entry, dict):
        raise ValidationError("Bangumi 返回的收藏数据不完整，请稍后重新获取。")
    if type(entry.get("private")) is not bool:
        raise ValidationError("Bangumi 返回的收藏数据不完整，请稍后重新获取。")
    subject_type = _integer(entry.get("subject_type"), minimum=1)
    if entry.get("private") is True or subject_type not in {1, 2, 4, 6}:
        return None
    subject_id = _integer(entry.get("subject_id"), minimum=1)
    subject = entry.get("subject")
    if subject is None:
        return None
    if (
        not isinstance(subject, dict)
        or subject.get("id") != subject_id
        or subject.get("type") != subject_type
    ):
        raise ValidationError("Bangumi 返回的作品资料不完整，请稍后重新获取。")
    if subject_type == 1:
        try:
            subject = bangumi.subject(str(subject_id))
        except services.ProviderAPIError as error:
            if error.status_code in {403, 404}:
                return None
            raise
        if (
            not isinstance(subject, dict)
            or subject.get("id") != subject_id
            or subject.get("type") != subject_type
            or not isinstance(subject.get("platform"), str)
        ):
            raise ValidationError("Bangumi 返回的书籍类型不完整，请稍后重新获取。")
    kind = bangumi.media_type(subject)
    rate = _integer(entry.get("rate"), maximum=10)
    status = _integer(entry.get("type"), minimum=1, maximum=5)
    episodes = _integer(entry.get("ep_status"))
    volumes = _integer(entry.get("vol_status"))
    position = []
    if kind in {"book", "manga"}:
        if volumes:
            position.append(f"已读 {volumes} 卷")
        if episodes:
            unit = "话" if kind == "manga" else "章"
            position.append(f"已读 {episodes} {unit}")
    elif kind in {"anime", "tv"} and episodes:
        position.append(f"已看 {episodes} 集")
    images = subject.get("images") or {}
    if not isinstance(images, dict):
        raise ValidationError("Bangumi 返回的封面资料不完整，请稍后重新获取。")
    return {
        "source": "bangumi",
        "media_type": kind,
        "media_id": str(subject_id),
        "title": subject.get("name_cn") or subject.get("name"),
        "image": bangumi.image_url(subject),
        "score": rate if rate else None,
        "status": STATUS_MAP[status],
        "notes": entry.get("comment") or "",
        "viewing_url": "",
        "position": " · ".join(position),
        "folders": [],
        "progress_minutes": 0 if kind == "game" else None,
    }


def fetch_collection(username):
    """Read every public page before returning a validated import payload."""
    offset = 0
    works = []
    skipped = 0
    payload_size = 0
    expected_total = None
    path = f"users/{quote(username, safe='')}/collections"
    while True:
        try:
            page = bangumi.request(path, params={"limit": PAGE_SIZE, "offset": offset})
        except services.ProviderAPIError as error:
            if error.status_code == 404:
                raise ValidationError(
                    "未找到该 Bangumi 用户，请核对个人主页 /user/ 后的用户名。"
                ) from error
            raise
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ValidationError("Bangumi 返回的收藏列表不完整，请稍后重新获取。")
        total = _integer(page.get("total"))
        if total > MAX_COLLECTIONS:
            raise ValidationError("单次最多读取 10000 条公开收藏。")
        if expected_total is None:
            expected_total = total
        entries = page["data"]
        if (
            _integer(page.get("offset")) != offset
            or total != expected_total
            or len(entries) > PAGE_SIZE
            or (not entries and offset < total)
            or offset + len(entries) > total
        ):
            raise ValidationError("Bangumi 收藏分页已变化，请重新获取完整列表。")
        for entry in entries:
            work = _work(entry)
            if work is None:
                skipped += 1
                continue
            payload_size += len(json.dumps(work, ensure_ascii=True).encode("utf-8"))
            if payload_size > library_transfer.MAX_FILE_BYTES:
                raise ValidationError("收藏数据超过单次导入的 5 MiB 限制。")
            works.append(work)
        offset += len(entries)
        if offset >= total:
            break
    return (
        library_transfer.validate_payload(
            {
                "format": library_transfer.FORMAT,
                "version": 1,
                "folders": [],
                "works": works,
            }
        ),
        skipped,
    )


def _pending(request):
    pending = request.session.get(SESSION_KEY)
    if not isinstance(pending, dict):
        return None
    created = pending.get("created")
    if (
        pending.get("owner") != str(request.user.pk)
        or type(created) not in {int, float}
        or not 0 <= time.time() - created <= PREVIEW_SECONDS
    ):
        return None
    return pending


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def bangumi_import(request):
    """Fetch, review and explicitly confirm one account's public collections."""
    form = UsernameForm()
    context = {}
    status = 200
    try:
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "preview":
                if request.session.pop(SESSION_KEY, None) is not None:
                    # SessionMiddleware does not save sessions on an upstream 5xx.
                    request.session.save()
                form = UsernameForm(request.POST)
                if form.is_valid():
                    username = form.cleaned_data["username"]
                    payload, skipped = fetch_collection(username)
                    request.session[SESSION_KEY] = {
                        "owner": str(request.user.pk),
                        "token": secrets.token_urlsafe(32),
                        "created": time.time(),
                        "username": username,
                        "payload": payload,
                        "skipped": skipped,
                    }
                else:
                    status = 400
            elif action == "confirm":
                pending = _pending(request)
                if not pending or not secrets.compare_digest(
                    str(pending.get("token", "")).encode("utf-8"),
                    request.POST.get("token", "").encode("utf-8"),
                ):
                    raise ValidationError("导入预览已失效，请重新获取收藏。")
                payload = library_transfer.validate_payload(pending["payload"])
                created = library_transfer.import_payload(request.user, payload)
                request.session.pop(SESSION_KEY, None)
                messages.success(request, f"Bangumi 导入完成，新增 {created} 件作品。")
                return redirect("library_bangumi")
            else:
                raise ValidationError("无效的导入操作。")
        pending = _pending(request)
        if pending:
            context.update(
                preview=library_transfer._preview(request.user, pending["payload"]),
                token=pending["token"],
                username=pending["username"],
                skipped=pending["skipped"],
                works=[
                    {
                        **work,
                        "kind_label": KINDS[work["media_type"]],
                        "status_label": STATES.get(work["status"], "未设置"),
                    }
                    for work in pending["payload"]["works"]
                ],
            )
    except ValidationError as error:
        context["error"] = error.messages[0]
        status = 400
    except services.ProviderAPIError as error:
        context["error"] = (
            "Bangumi 暂时限制请求，请稍后重新获取。"
            if error.status_code == 429
            else "暂时无法获取完整的 Bangumi 收藏，请检查网络后重试。"
        )
        status = 502
    context["form"] = form
    return render(request, "app/library/bangumi.html", context, status=status)
