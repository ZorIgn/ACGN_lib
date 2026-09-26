"""Portable exports and owner-scoped, reviewed library imports."""

import csv
import io
import json
import secrets
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models, transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from app.library import KINDS, STATES, model_for
from app.models import Item, LibraryFolder, LibraryLink, Sources

MAX_FILE_BYTES = 5 * 1024 * 1024
SESSION_KEY = "library_transfer_preview"
FORMAT = "acglib-library"
VERSION = 1
WORK_FIELDS = {
    "source",
    "media_type",
    "media_id",
    "title",
    "image",
    "score",
    "status",
    "notes",
    "viewing_url",
    "position",
    "folders",
    "progress_minutes",
}


def _identity(work):
    return work["source"], work["media_type"], work["media_id"]


def export_payload(user):
    """Collect the owner's current works and folder names without provider requests."""
    folders = list(LibraryFolder.objects.filter(user=user).prefetch_related("items"))
    memberships = {}
    for folder in folders:
        for item in folder.items.all():
            memberships.setdefault(item.pk, []).append(folder.name)
    links = {link.item_id: link for link in LibraryLink.objects.filter(user=user)}
    works = {}
    for kind in KINDS:
        records = (
            model_for(kind)
            .objects.filter(user=user)
            .select_related("item")
            .order_by("-created_at", "-pk")
        )
        for record in records:
            item = record.item
            key = (item.source, kind, item.media_id)
            if key in works:
                continue
            link = links.get(item.pk)
            works[key] = {
                "source": item.source,
                "media_type": kind,
                "media_id": item.media_id,
                "title": item.title,
                "image": item.image,
                "score": format(record.score, ".1f")
                if record.score is not None
                else None,
                "status": record.status,
                "notes": record.notes,
                "viewing_url": link.url if link else "",
                "position": link.position if link else "",
                "folders": sorted(memberships.get(item.pk, [])),
                "progress_minutes": record.progress if kind == "game" else None,
            }
    return {
        "format": FORMAT,
        "version": VERSION,
        "folders": sorted(folder.name for folder in folders),
        "works": [works[key] for key in sorted(works)],
    }


def _text(value, label, limit, *, required=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ValidationError(f"{label}格式无效，最多允许 {limit} 个字符。")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValidationError(f"{label}包含无效字符。") from error
    if required and not value.strip():
        raise ValidationError(f"{label}不能为空。")
    return value


def _names(value, label):
    if not isinstance(value, list):
        raise ValidationError(f"{label}必须是名称列表。")
    names = [_text(name, label, 80, required=True) for name in value]
    if len(names) != len(set(names)):
        raise ValidationError(f"{label}存在重复名称。")
    return sorted(names)


def _url(value, label, *, image=False):
    value = _text(value, label, 2000)
    if not value:
        return value
    if any(ord(character) < 32 for character in value) or "\\" in value:
        raise ValidationError(f"{label}必须是有效的 HTTP 或 HTTPS 地址。")
    if image and value.startswith("/") and not value.startswith("//"):
        return value
    try:
        URLValidator(schemes=["http", "https"])(value)
    except ValidationError as error:
        raise ValidationError(f"{label}必须是有效的 HTTP 或 HTTPS 地址。") from error
    return value


def _score(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValidationError("评分必须为 0–10 之间、最多一位小数的数字，或 null。")
    try:
        score = Decimal(str(value))
        if (
            not score.is_finite()
            or not 0 <= score <= 10
            or score != score.quantize(Decimal("0.1"))
        ):
            raise InvalidOperation
    except (InvalidOperation, ValueError) as error:
        raise ValidationError(
            "评分必须为 0–10 之间、最多一位小数的数字，或 null。"
        ) from error
    return format(score, ".1f")


def validate_payload(payload):
    """Validate every entry before returning a normalized, JSON-safe payload."""
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise ValidationError("不支持此文件格式，请选择 ACGLib 导出的 JSON 文件。")
    if type(payload.get("version")) is not int or payload["version"] != VERSION:
        raise ValidationError("不支持此备份版本，请使用版本 1 的 ACGLib JSON 文件。")
    if set(payload) != {"format", "version", "folders", "works"}:
        raise ValidationError("JSON 文件字段不完整或包含未知字段。")
    folders = _names(payload["folders"], "文件夹名称")
    if not isinstance(payload["works"], list):
        raise ValidationError("作品数据必须是列表。")
    works = []
    for index, entry in enumerate(payload["works"], 1):
        try:
            if not isinstance(entry, dict) or set(entry) != WORK_FIELDS:
                raise ValidationError("作品字段不完整或包含未知字段。")
            if (
                not isinstance(entry["source"], str)
                or entry["source"] not in Sources.values
            ):
                raise ValidationError("作品来源无效。")
            if (
                not isinstance(entry["media_type"], str)
                or entry["media_type"] not in KINDS
            ):
                raise ValidationError("作品类型无效。")
            if not isinstance(entry["status"], str) or entry["status"] not in {
                "",
                *STATES,
            }:
                raise ValidationError("作品状态无效。")
            work = dict(entry)
            work["media_id"] = _text(entry["media_id"], "作品标识", 36, required=True)
            work["title"] = _text(entry["title"], "作品标题", 10000, required=True)
            work["image"] = _url(entry["image"], "封面地址", image=True)
            work["score"] = _score(entry["score"])
            work["notes"] = _text(entry["notes"], "个人记录", 10000)
            work["viewing_url"] = _url(entry["viewing_url"], "观看 / 阅读链接")
            work["position"] = _text(entry["position"], "观看 / 阅读位置", 120)
            work["folders"] = _names(entry["folders"], "作品文件夹")
            if not set(work["folders"]).issubset(folders):
                raise ValidationError("作品引用了未声明的文件夹。")
            progress = entry["progress_minutes"]
            if entry["media_type"] == "game":
                if type(progress) is not int or not 0 <= progress <= 2147483647:
                    raise ValidationError("游戏累计时长必须为有效的非负整数分钟数。")
            elif progress is not None:
                raise ValidationError("仅游戏作品可以设置累计时长。")
            works.append(work)
        except ValidationError as error:
            raise ValidationError(f"第 {index} 件作品：{error.messages[0]}") from error
    return {"format": FORMAT, "version": VERSION, "folders": folders, "works": works}


def _json_object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = child
    return value


def _invalid_constant(value):
    raise ValueError("Invalid JSON number")


def _read_upload(upload):
    if upload is None:
        raise ValidationError("请选择要导入的 JSON 文件。")
    if upload.size > MAX_FILE_BYTES:
        raise ValidationError("文件不能超过 5 MiB。")
    content = upload.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValidationError("文件不能超过 5 MiB。")
    try:
        payload = json.loads(
            content.decode("utf-8-sig"),
            object_pairs_hook=_json_object,
            parse_constant=_invalid_constant,
            parse_float=Decimal,
        )
    except (UnicodeDecodeError, ValueError, RecursionError, InvalidOperation) as error:
        raise ValidationError("文件已损坏或不是有效的 UTF-8 JSON 文件。") from error
    return validate_payload(payload)


def _owned_identities(user):
    identities = set()
    for kind in KINDS:
        identities.update(
            (source, kind, media_id)
            for source, media_id in model_for(kind)
            .objects.filter(user=user)
            .values_list("item__source", "item__media_id")
        )
    return identities


def _preview(user, payload):
    existing = _owned_identities(user)
    unique = {_identity(work) for work in payload["works"]}
    current_folders = set(
        LibraryFolder.objects.filter(user=user).values_list("name", flat=True)
    )
    return {
        "total": len(payload["works"]),
        "new": len(unique - existing),
        "existing": len(unique & existing),
        "duplicates": len(payload["works"]) - len(unique),
        "new_folders": len(set(payload["folders"]) - current_folders),
    }


@transaction.atomic
def import_payload(user, payload):
    """Add new works atomically while preserving all existing owner records."""
    get_user_model().objects.select_for_update().get(pk=user.pk)
    existing = _owned_identities(user)
    folders = {
        name: LibraryFolder.objects.get_or_create(user=user, name=name)[0]
        for name in payload["folders"]
    }
    created = 0
    for work in payload["works"]:
        key = _identity(work)
        if key in existing:
            continue
        item, _ = Item.objects.get_or_create(
            source=work["source"],
            media_type=work["media_type"],
            media_id=work["media_id"],
            season_number=None,
            episode_number=None,
            defaults={"title": work["title"], "image": work["image"]},
        )
        record = model_for(work["media_type"])(
            user=user,
            item=item,
            score=work["score"],
            status=work["status"],
            notes=work["notes"],
        )
        if work["media_type"] == "game":
            record.progress = work["progress_minutes"]
        # Provider hooks must not reinterpret the owner's exported status or progress.
        models.Model.save(record)
        if work["source"] == Sources.STEAM and work["media_type"] == "game":
            # Steam synchronization recognizes status updates as personal decisions.
            models.Model.save(record, update_fields=["status"])
        if work["viewing_url"] or work["position"] or work["source"] == Sources.STEAM:
            LibraryLink.objects.update_or_create(
                user=user,
                item=item,
                defaults={"url": work["viewing_url"], "position": work["position"]},
            )
        for name in work["folders"]:
            folders[name].items.add(item)
        existing.add(key)
        created += 1
    return created


def _csv_cell(value):
    value = "" if value is None else str(value)
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(
        ("=", "+", "-", "@")
    ):
        return "'" + value
    return value


def _export(user, file_format):
    payload = export_payload(user)
    if file_format == "json":
        response = HttpResponse(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            content_type="application/json; charset=utf-8",
        )
    else:
        output = io.StringIO(newline="")
        writer = csv.writer(output, quoting=csv.QUOTE_ALL)
        columns = [
            ("source", "来源"),
            ("media_type", "类型"),
            ("media_id", "作品标识"),
            ("title", "标题"),
            ("image", "封面地址"),
            ("score", "评分"),
            ("status", "状态"),
            ("notes", "个人记录"),
            ("viewing_url", "观看 / 阅读链接"),
            ("position", "观看 / 阅读位置"),
            ("folders", "收藏文件夹"),
            ("progress_minutes", "游戏累计时长（分钟）"),
        ]
        writer.writerow([label for _, label in columns])
        for work in payload["works"]:
            cells = {
                **work,
                "media_type": KINDS[work["media_type"]],
                "status": STATES.get(work["status"], "未设置"),
                "folders": "\n".join(work["folders"]),
            }
            writer.writerow([_csv_cell(cells[key]) for key, _ in columns])
        response = HttpResponse(
            "\ufeff" + output.getvalue(), content_type="text/csv; charset=utf-8"
        )
    response["Content-Disposition"] = (
        f'attachment; filename="acglib-library.{file_format}"'
    )
    return response


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def transfer(request):
    """Export private data or review and confirm one server-stored import."""
    context = {}
    status = 200
    try:
        if request.method == "GET" and "format" in request.GET:
            file_format = request.GET["format"]
            if file_format not in {"json", "csv"}:
                raise ValidationError("请选择 JSON 或 CSV 导出格式。")
            return _export(request.user, file_format)
        if request.method == "POST":
            action = request.POST.get("action")
            if action == "preview":
                request.session.pop(SESSION_KEY, None)
                payload = _read_upload(request.FILES.get("file"))
                pending = {
                    "owner": str(request.user.pk),
                    "token": secrets.token_urlsafe(32),
                    "payload": payload,
                }
                request.session[SESSION_KEY] = pending
            elif action == "confirm":
                pending = request.session.get(SESSION_KEY)
                if (
                    not pending
                    or pending.get("owner") != str(request.user.pk)
                    or not secrets.compare_digest(
                        str(pending.get("token", "")).encode("utf-8"),
                        request.POST.get("token", "").encode("utf-8"),
                    )
                ):
                    raise ValidationError("导入预览已失效，请重新选择文件并预览。")
                payload = validate_payload(pending["payload"])
                created = import_payload(request.user, payload)
                request.session.pop(SESSION_KEY, None)
                messages.success(request, f"导入完成，新增 {created} 件作品。")
                return redirect("library_transfer")
            else:
                raise ValidationError("无效的数据操作。")
        pending = request.session.get(SESSION_KEY)
        if pending and pending.get("owner") == str(request.user.pk):
            context.update(
                preview=_preview(request.user, pending["payload"]),
                token=pending["token"],
            )
    except ValidationError as error:
        context["error"] = error.messages[0]
        status = 400
    return render(request, "app/library/transfer.html", context, status=status)
