"""User-triggered capture of a browser page's title and reading link."""

import json

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.validators import URLValidator
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from app import library
from app.models import LibraryImportDraft


class ClipForm(forms.Form):
    title = forms.CharField(label="作品名称", max_length=500)
    media_type = forms.ChoiceField(label="作品类型", choices=library.KINDS.items())
    url = forms.URLField(label="当前页面链接", max_length=2000, assume_scheme="https")

    def clean_url(self):
        url = self.cleaned_data["url"]
        URLValidator(schemes=["http", "https"])(url)
        return url


@login_required
@require_http_methods(["GET", "POST"])
def clip(request):
    form = ClipForm(
        request.POST if request.method == "POST" else None,
        initial={
            "title": request.GET.get("title", "")[:500],
            "url": request.GET.get("url", "")[:2000],
            "media_type": "book",
        },
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        row = library.lookup(
            {"input": data["title"], "media_type": data["media_type"], "notes": ""}
        )
        row["viewing_url"] = data["url"]
        draft = LibraryImportDraft.objects.create(
            user=request.user,
            title=data["title"][:200],
            entries=[row],
            return_url=library.return_url(request.get_full_path(), "/library/clip/"),
        )
        return redirect(library.draft_url(draft))
    address = json.dumps(request.build_absolute_uri(reverse("library_clip")))
    bookmarklet = (
        "javascript:(()=>{if(!/^https?:$/.test(location.protocol))return;"
        f"const u=new URL({address});"
        "u.searchParams.set('title',document.title.slice(0,500));"
        "u.searchParams.set('url',location.href);"
        "window.open(u.href,'_blank','noopener');})()"
    )
    return render(
        request, "app/library/clip.html", {"form": form, "bookmarklet": bookmarklet}
    )
