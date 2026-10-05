"""Private discovery fragments and reviewed additions to the bookshelf."""

from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.paginator import Paginator
from django.db import models, transaction
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from app import library, recommendations
from app.models import Item, LibraryImportDraft, LibraryRecommendationDismissal

TOKEN_SALT = "library-recommendation"
CANDIDATE_FIELDS = (
    "source",
    "media_type",
    "media_id",
    "title",
    "original_title",
    "image",
    "author",
    "format",
    "year",
)


@login_required
@require_GET
@never_cache
def suggestions(request):
    """Render live preferences without blocking the search form's first load."""
    kind = request.GET.get("type", "book")
    mode = request.GET.get("mode", "personal")
    if kind not in recommendations.KINDS or mode not in {"personal", "popular"}:
        raise Http404
    result = recommendations.batch(
        request.user, kind, mode, refresh=request.GET.get("refresh") == "1"
    )
    page = Paginator(result["items"], 12).get_page(request.GET.get("page"))
    for item in result["items"]:
        candidate = {key: item.get(key, "") for key in CANDIDATE_FIELDS}
        candidate["kind_label"] = library.KINDS[kind]
        item["token"] = signing.dumps(
            {"user": request.user.pk, "candidate": candidate},
            salt=TOKEN_SALT,
            compress=True,
        )
    return render(
        request,
        "app/library/recommendations.html",
        {
            **result,
            "items": result["items"],
            "recommend_page": page,
            "mode": mode,
            "kind_label": library.KINDS[kind],
            "return_url": f"/library/add/?type={kind}&mode={mode}&recommend_page={page.number}",
        },
    )


def verified_candidate(request):
    """Read one signed catalog candidate belonging to the current user."""
    data = signing.loads(
        request.POST.get("candidate", ""), salt=TOKEN_SALT, max_age=86400
    )
    candidate = data["candidate"]
    if (
        data["user"] != request.user.pk
        or not isinstance(candidate, dict)
        or candidate.get("media_type") not in recommendations.KINDS
        or candidate.get("source") not in {"bangumi", "webnovel", "tmdb"}
        or not isinstance(candidate.get("media_id"), str)
        or not 1 <= len(candidate["media_id"]) <= 100
        or not isinstance(candidate.get("title"), str)
        or not 1 <= len(candidate["title"]) <= 500
    ):
        raise signing.BadSignature
    return candidate


@login_required
@require_POST
def feedback(request):
    """Persist or undo a user's dismissal of a verified recommendation."""
    try:
        candidate = verified_candidate(request)
    except (signing.BadSignature, KeyError, TypeError):
        return JsonResponse({"error": "推荐已过期或无效，请刷新后重试。"}, status=400)
    action = request.POST.get("action")
    identity = {
        "user": request.user,
        "source": candidate["source"],
        "media_type": candidate["media_type"],
        "media_id": candidate["media_id"],
    }
    if action == "dismiss":
        LibraryRecommendationDismissal.objects.get_or_create(
            **identity, defaults={"title": candidate["title"]}
        )
    elif action == "undo":
        LibraryRecommendationDismissal.objects.filter(**identity).delete()
    else:
        return JsonResponse({"error": "请选择有效的推荐操作。"}, status=400)
    return JsonResponse(
        {
            "ok": True,
            "action": action,
            "title": candidate["title"],
            "candidate": request.POST["candidate"],
        }
    )


@login_required
@require_POST
def choose(request):
    """Open one verified recommendation in the existing import review."""
    try:
        candidate = verified_candidate(request)
    except (signing.BadSignature, KeyError, TypeError):
        return HttpResponseBadRequest("推荐已过期或无效，请返回搜索页刷新后重试。")
    if request.POST.get("action") == "quick_add":
        form = library.PersonalRecordForm(request.POST, prefix="quick")
        if not form.is_valid():
            return JsonResponse(
                {
                    "error": "；".join(
                        str(error)
                        for errors in form.errors.values()
                        for error in errors
                    )
                },
                status=400,
            )
        with transaction.atomic():
            item, _ = Item.objects.get_or_create(
                source=candidate["source"],
                media_type=candidate["media_type"],
                media_id=candidate["media_id"],
                defaults={"title": candidate["title"], "image": candidate["image"]},
            )
            model = library.model_for(candidate["media_type"])
            created = not model.objects.filter(user=request.user, item=item).exists()
            if created:
                record = model(
                    user=request.user,
                    item=item,
                    score=form.cleaned_data["score"],
                    status=form.cleaned_data["status"],
                )
                models.Model.save(record)
        return JsonResponse(
            {"ok": True, "created": created, "title": candidate["title"]}
        )
    row = {
        "input": candidate["title"],
        "media_type": candidate["media_type"],
        "notes": "",
        "viewing_url": "",
        "choices": [candidate],
        "selected": [0],
        "manual_title": candidate["title"],
        "manual": {
            "source": "manual",
            "media_type": candidate["media_type"],
            "title": candidate["title"],
            "image": candidate["image"],
        },
    }
    draft = LibraryImportDraft.objects.create(
        user=request.user,
        title=candidate["title"][:150],
        entries=[row],
        return_url=library.return_url(request.POST.get("next")),
    )
    return redirect(library.draft_url(draft))
