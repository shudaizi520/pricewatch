"""Administrator settings, Feishu connection and downloadable backups."""

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr
from sqlalchemy import select

from pricewatch.db.models import Administrator, BackupRecord, Setting
from pricewatch.services.auth import hash_password, verify_password
from pricewatch.services.crypto import SecretBox
from pricewatch.services.notifications import NotificationService, normalize_feishu_destination
from pricewatch.web.dependencies import csrf_token, require_admin, verify_csrf
from pricewatch.web.forms import FormError, validate_password
from pricewatch.web.timezone import beijing_label

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")
templates.env.filters["beijing"] = beijing_label


@router.get("/settings")
async def settings_page(request: Request) -> Response:
    admin = require_admin(request)
    with request.app.state.session_factory() as session:
        configured = session.get(Setting, "feishu_webhook") is not None
        signing_configured = session.get(Setting, "feishu_signing_secret") is not None
        backups = session.scalars(
            select(BackupRecord).order_by(BackupRecord.created_at.desc()).limit(10)
        ).all()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "admin": admin,
            "csrf_token": csrf_token(request),
            "configured": configured,
            "signing_configured": signing_configured,
            "backups": backups,
            "message": None,
        },
    )


@router.post("/settings")
async def save_settings(
    request: Request,
    feishu_webhook: str = Form(),
    feishu_signing_secret: str = Form(""),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    require_admin(request)
    box = SecretBox(request.app.state.settings.app_secret_key.get_secret_value())
    with request.app.state.session_factory() as session:
        saved_webhook = session.get(Setting, "feishu_webhook")
        saved_signing = session.get(Setting, "feishu_signing_secret")
        webhook = feishu_webhook.strip() or (
            box.decrypt(saved_webhook.value_text)
            if saved_webhook is not None and saved_webhook.value_text
            else ""
        )
        signing = feishu_signing_secret.strip() or (
            box.decrypt(saved_signing.value_text)
            if saved_signing is not None and saved_signing.value_text
            else ""
        )
    if not webhook:
        raise HTTPException(422, "请先填写飞书 Webhook")
    try:
        normalize_feishu_destination(webhook)
    except ValueError as error:
        raise HTTPException(422, "飞书 Webhook 格式不正确") from error
    with request.app.state.session_factory.begin() as session:
        row = session.get(Setting, "feishu_webhook")
        if row is None:
            row = Setting(key="feishu_webhook")
            session.add(row)
        row.value_text = box.encrypt(webhook)
        if signing:
            signed = session.get(Setting, "feishu_signing_secret")
            if signed is None:
                signed = Setting(key="feishu_signing_secret")
                session.add(signed)
            signed.value_text = box.encrypt(signing)
    request.app.state.check_service.notifier = NotificationService(
        request.app.state.session_factory,
        SecretStr(webhook),
        signing_secret=SecretStr(signing) if signing else None,
    )
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/notifications/test")
async def notification_test(
    request: Request, submitted_csrf: str = Form(alias="csrf_token")
) -> Response:
    verify_csrf(request, submitted_csrf)
    require_admin(request)
    notifier = request.app.state.check_service.notifier
    if notifier is None:
        raise HTTPException(400, "请先保存飞书 Webhook")
    result = notifier.test_feishu()
    return RedirectResponse(
        "/settings?test=sent" if result.sent else "/settings?test=failed", status_code=303
    )


@router.post("/settings/password")
async def change_password(
    request: Request,
    current_password: str = Form(),
    new_password: str = Form(),
    submitted_csrf: str = Form(alias="csrf_token"),
) -> Response:
    verify_csrf(request, submitted_csrf)
    admin = require_admin(request)
    try:
        validated = validate_password(new_password)
    except FormError as error:
        raise HTTPException(422, str(error)) from error
    with request.app.state.session_factory.begin() as session:
        stored = session.get(Administrator, admin.id)
        assert stored is not None
        if not verify_password(current_password, stored.password_hash):
            raise HTTPException(403, "当前密码不正确")
        stored.password_hash = hash_password(validated)
    return RedirectResponse("/settings?password=changed", status_code=303)


@router.post("/backups/create")
async def backup_create(
    request: Request, submitted_csrf: str = Form(alias="csrf_token")
) -> dict[str, str]:
    verify_csrf(request, submitted_csrf)
    require_admin(request)
    record = request.app.state.backups.create("manual")
    return {"download_url": f"/backups/{record.id}/download", "checksum": record.checksum}


@router.get("/backups/{backup_id}/download")
async def backup_download(request: Request, backup_id: int) -> Response:
    require_admin(request)
    with request.app.state.session_factory() as session:
        record = session.get(BackupRecord, backup_id)
        if record is None:
            raise HTTPException(404)
        filename = record.filename
        checksum = record.checksum
    path = request.app.state.backups.directory / filename
    if path.name != filename or not path.is_file():
        raise HTTPException(404)
    if request.app.state.backups.verify(path).checksum != checksum:
        raise HTTPException(409, "备份校验失败")
    if path.stat().st_size > 64 * 1024 * 1024:
        raise HTTPException(413, "备份较大 请通过 TrueNAS 下载数据集")
    return Response(
        path.read_bytes(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
