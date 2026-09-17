"""Steam connection and sync controls for the local owner."""

import json

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from app.library import page_context
from app.library_tasks import sync_steam
from app.models import SteamConnection
from integrations.imports.helpers import encrypt


class SteamForm(forms.Form):
    steam_id = forms.RegexField(
        r"^7656119\d{10}$",
        label="SteamID64",
        max_length=17,
        error_messages={"invalid": "请输入以 7656119 开头的 17 位 SteamID64。"},
        widget=forms.TextInput(attrs={"inputmode": "numeric"}),
    )
    api_key = forms.RegexField(
        r"^[a-fA-F0-9]{32}$",
        label="Steam Web API 密钥",
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        error_messages={"invalid": "Steam API 密钥应为 32 位字母和数字。"},
    )
    automatic = forms.BooleanField(label="运行期间每天同步一次", required=False)


@require_http_methods(["GET", "POST"])
def connection(request):
    """Store credentials locally and enqueue a sync without exposing the key."""
    saved = SteamConnection.objects.filter(user=request.user).first()
    form = SteamForm(
        request.POST if request.method == "POST" else None,
        initial={
            "steam_id": saved.steam_id if saved else "",
            "automatic": saved.automatic if saved else False,
        },
    )
    if request.method == "POST" and form.is_valid():
        if saved and saved.running:
            messages.info(request, "同步正在进行，请完成后再修改连接。")
            return redirect("library_steam")
        key = form.cleaned_data["api_key"]
        if not key and not saved:
            form.add_error("api_key", "首次连接请填写 API 密钥。")
        else:
            saved, _ = SteamConnection.objects.update_or_create(
                user=request.user,
                defaults={
                    "steam_id": form.cleaned_data["steam_id"],
                    "encrypted_key": encrypt(key) if key else saved.encrypted_key,
                    "automatic": form.cleaned_data["automatic"],
                },
            )
            interval, _ = IntervalSchedule.objects.get_or_create(
                every=1, period=IntervalSchedule.DAYS
            )
            PeriodicTask.objects.update_or_create(
                name=f"library-steam-{request.user.pk}",
                defaults={
                    "task": "app.library_tasks.sync_steam",
                    "interval": interval,
                    "args": json.dumps([request.user.pk]),
                    "enabled": saved.automatic,
                },
            )
            if request.POST.get("action") == "sync":
                SteamConnection.objects.filter(pk=saved.pk).update(
                    result="正在等待同步…"
                )
                sync_steam.delay(request.user.pk)
                messages.success(request, "同步已提交。稍后刷新查看结果。")
            else:
                messages.success(request, "连接设置已保存。")
            return redirect("library_steam")
    return render(
        request, "app/library/steam.html", page_context(form=form, connection=saved)
    )
