"""Apply one explicit change to an owner's selected shelf records."""

import json
import re
from collections import defaultdict

from django import forms
from django.contrib.auth.decorators import login_required
from django.db import models, transaction
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from app.library import KINDS, STATES, model_for
from app.models import LibraryFolder


class SelectionForm(forms.Form):
    action = forms.ChoiceField(
        choices=[(value, value) for value in ["status", "folder_add", "folder_remove"]]
    )
    selection = forms.CharField(max_length=60000)
    status = forms.ChoiceField(
        required=False, choices=[("", "未设置"), *STATES.items()]
    )
    folder = forms.IntegerField(required=False, min_value=1, max_value=2**63 - 1)

    def clean_selection(self):
        try:
            selection = json.loads(self.cleaned_data["selection"])
        except (ValueError, TypeError) as error:
            raise forms.ValidationError("作品选择无效，请重新勾选。") from error
        if not isinstance(selection, list) or not 1 <= len(selection) <= 2000:
            raise forms.ValidationError("请勾选 1–2000 部作品。")
        grouped = defaultdict(set)
        for value in selection:
            match = (
                re.fullmatch(r"([a-z]+):([1-9]\d{0,18})", value)
                if isinstance(value, str)
                else None
            )
            if not match or match[1] not in KINDS or int(match[2]) > 2**63 - 1:
                raise forms.ValidationError("作品选择无效，请重新勾选。")
            grouped[match[1]].add(int(match[2]))
        return grouped

    def clean(self):
        data = super().clean()
        if data.get("action", "").startswith("folder_") and not data.get("folder"):
            self.add_error("folder", "请选择文件夹。")
        return data


@login_required
@require_POST
def edit(request):
    form = SelectionForm(request.POST)
    if not form.is_valid():
        return JsonResponse(
            {
                "error": "；".join(
                    str(error) for errors in form.errors.values() for error in errors
                )
            },
            status=400,
        )
    action = form.cleaned_data["action"]
    with transaction.atomic():
        records = []
        for kind, keys in form.cleaned_data["selection"].items():
            selected = list(
                model_for(kind).objects.filter(user=request.user, pk__in=keys)
            )
            if len(selected) != len(keys):
                return JsonResponse(
                    {"error": "部分作品已不在你的书架中，请刷新后重新选择。"},
                    status=400,
                )
            records.extend(selected)
        if action == "status":
            for record in records:
                record.status = form.cleaned_data["status"]
                models.Model.save(record, update_fields=["status"])
        else:
            folder = LibraryFolder.objects.filter(
                user=request.user, pk=form.cleaned_data["folder"]
            ).first()
            if folder is None:
                return JsonResponse(
                    {"error": "文件夹不存在，请刷新后重试。"}, status=400
                )
            item_ids = {record.item_id for record in records}
            if action == "folder_add":
                folder.items.add(*item_ids)
            else:
                folder.items.remove(*item_ids)
    return JsonResponse(
        {
            "ok": True,
            "count": len(records),
            "message": f"已更新 {len(records)} 部作品。",
        }
    )
