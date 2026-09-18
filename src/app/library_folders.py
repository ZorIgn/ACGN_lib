"""Owner-scoped bookshelf folder management."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from app.library import return_url
from app.models import LibraryFolder


class FolderForm(forms.Form):
    name = forms.CharField(label="文件夹名称", max_length=80)


@login_required
@require_POST
def manage(request):
    action = request.POST.get("action")
    if action not in {"create", "rename", "delete"}:
        return HttpResponseBadRequest("无效的文件夹操作。")
    folder = None
    if action != "create":
        folder_id = request.POST.get("folder_id", "")
        if not folder_id.isdecimal():
            return HttpResponseBadRequest("无效的文件夹。")
        folder = get_object_or_404(LibraryFolder, user=request.user, pk=folder_id)
    destination = return_url(request.POST.get("next"), "/library/")
    if action == "delete":
        folder.delete()
        messages.success(request, "文件夹已删除，作品仍在书架中。")
        return redirect("library")
    form = FolderForm(request.POST)
    if not form.is_valid():
        messages.error(request, "文件夹名称需为 1–80 个字符。")
        return redirect(destination)
    try:
        with transaction.atomic():
            if action == "create":
                folder, _ = LibraryFolder.objects.get_or_create(
                    user=request.user, name=form.cleaned_data["name"]
                )
            else:
                folder.name = form.cleaned_data["name"]
                folder.save(update_fields=["name"])
    except IntegrityError:
        messages.error(request, "已有同名文件夹，请换一个名称。")
        return redirect(destination)
    return redirect(f"/library/?folder={folder.pk}")
