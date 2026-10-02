"""本地面板的账户设置：写入 .env，GET 不回密钥和完整地址。"""
import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from parallax_hedge.config import Settings, mask_identifier


ADDRESS = "0xe24d" + ("a" * 32) + "7777"
OTHER_ADDRESS = "0x" + ("b" * 36) + "9999"
API_KEY = "ab" * 32
PRIVATE = "unit-test-arcus-private-material"
LIGHTER_PRIVATE = "unit-test-lighter-private-material"
PROXY = "socks5://unit-user:p#ss@10.1.2.3:1080"
KEY_FILE = "keys/unit-test-private.pem"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def endpoint(app, method, path):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in (getattr(route, "methods", None) or ()):
            return route.endpoint
    raise LookupError(f"{method} {path}")


class Client:
    def __init__(self):
        self.rebinds = 0

    async def rebind_clients(self):
        self.rebinds += 1

    async def aclose(self):
        pass

    async def common_markets(self, force=False):
        return []


def make_app(tmp):
    from parallax_hedge.api import create_app

    root = Path(tmp)
    (root / "data").mkdir(exist_ok=True)
    settings = Settings(env_path=root / ".env", data_dir=root / "data")
    app = create_app(settings)
    app.state.service.client = Client()
    return app


def http_error(coro):
    with pytest.raises(Exception) as info:
        run(coro)
    return getattr(info.value, "status_code", None), str(getattr(info.value, "detail", info.value))


def strings(payload):
    blob = json.dumps(payload, ensure_ascii=False)
    found = [blob]
    def walk(obj):
        if isinstance(obj, dict):
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)
        elif isinstance(obj, str):
            found.append(obj)
    walk(payload)
    return found


def assert_redacted(payload, *secrets):
    blob = "\n".join(strings(payload))
    for secret in secrets:
        assert secret not in blob


def field_map(payload):
    return {item["key"]: item for item in payload["fields"]}


def test_mask_keeps_the_head_and_tail_of_a_wallet_address():
    assert len(ADDRESS) == 42
    assert mask_identifier(ADDRESS) == "0xe24d…7777"
    assert mask_identifier("") == ""
    assert ADDRESS not in mask_identifier(ADDRESS)


def test_empty_account_fields_render_blank_and_short_indexes_stay_visible():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        view = run(endpoint(app, "GET", "/api/account-settings")())
        fields = field_map(view)
        assert fields["ARCUS_API_PRIVATE_KEY"]["display"] == ""
        assert fields["ARCUS_API_PRIVATE_KEY"]["value"] == ""
        assert fields["ARCUS_ADDRESS"]["display"] == ""
        assert fields["ARCUS_ADDRESS"]["value"] == ""
        assert fields["LIGHTER_API_PRIVATE_KEY"]["set"] is False
        assert fields["ARCUS_ACCOUNT_INDEX"]["value"] == "0"
        assert fields["ARCUS_ACCOUNT_INDEX"]["display"] == "0"
        assert fields["LIGHTER_ACCOUNT_INDEX"]["value"] == ""
        assert fields["LIGHTER_API_KEY_INDEX"]["value"] == ""
        keys = {item["key"] for item in view["fields"]}
        assert keys == {
            "ARCUS_API_URL", "ARCUS_ADDRESS", "ARCUS_ACCOUNT_INDEX", "ARCUS_API_KEY",
            "ARCUS_API_PRIVATE_KEY", "ARCUS_API_PRIVATE_KEY_FILE",
            "LIGHTER_BASE_URL", "LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX",
            "LIGHTER_API_PRIVATE_KEY", "ARCUS_PROXY", "LIGHTER_PROXY", "GLOBAL_PROXY",
        }
        app.state.store.close()


def test_save_writes_dotenv_but_get_never_returns_secrets_or_full_identifiers():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        env = app.state.settings.env_path
        env.write_text("DRY_RUN=true\nUNRELATED=keep-me\n", encoding="utf-8")
        save = endpoint(app, "POST", "/api/account-settings")
        app.state.engine.executor._arcus_key = object()
        app.state.engine.executor._arcus_api_key = "cached-public"
        app.state.engine.executor._lighter_signer = object()
        body = run(save({
            "ARCUS_ADDRESS": ADDRESS.upper(),
            "ARCUS_ACCOUNT_INDEX": "2",
            "ARCUS_API_KEY": "0x" + API_KEY.upper(),
            "ARCUS_API_PRIVATE_KEY": PRIVATE,
            "ARCUS_API_PRIVATE_KEY_FILE": KEY_FILE,
            "LIGHTER_ACCOUNT_INDEX": "22370",
            "LIGHTER_API_KEY_INDEX": "4",
            "LIGHTER_API_PRIVATE_KEY": LIGHTER_PRIVATE,
            "ARCUS_PROXY": PROXY,
            "DRY_RUN": "false",
        }))
        assert body["restart_required"] is False
        assert body["persisted"] is True
        assert "DRY_RUN" not in body["changed"]
        settings = app.state.settings
        assert settings.arcus_address == ADDRESS
        assert settings.arcus_account_index == 2
        assert settings.arcus_api_key == API_KEY
        assert settings.arcus_api_private_key == PRIVATE
        assert settings.lighter_account_index == 22370
        assert settings.lighter_api_key_index == 4
        assert settings.lighter_api_private_key == LIGHTER_PRIVATE
        assert settings.arcus_proxy == PROXY
        assert settings.arcus_api_private_key_file.endswith("keys/unit-test-private.pem")
        assert app.state.engine.executor.settings is settings
        assert app.state.engine.executor._arcus_key is None
        assert app.state.engine.executor._arcus_api_key is None
        assert app.state.engine.executor._lighter_signer is None
        assert app.state.reconciler.account_index == 22370
        assert app.state.service.client.rebinds == 1
        assert_redacted(body, PRIVATE, LIGHTER_PRIVATE, API_KEY, ADDRESS, PROXY, "p#ss", KEY_FILE)
        fields = field_map(body)
        assert fields["ARCUS_API_PRIVATE_KEY"]["display"] == "****"
        assert fields["ARCUS_API_PRIVATE_KEY"]["value"] == ""
        assert fields["ARCUS_API_KEY"]["display"] == "****"
        assert fields["LIGHTER_API_PRIVATE_KEY"]["display"] == "****"
        assert fields["ARCUS_PROXY"]["display"] == "****"
        assert fields["ARCUS_ADDRESS"]["display"] == "0xe24d…7777"
        assert fields["ARCUS_ADDRESS"]["value"] == ""
        assert fields["ARCUS_API_PRIVATE_KEY_FILE"]["value"] == ""
        assert "…" in fields["ARCUS_API_PRIVATE_KEY_FILE"]["display"]
        assert fields["ARCUS_ACCOUNT_INDEX"]["value"] == "2"
        assert fields["LIGHTER_ACCOUNT_INDEX"]["value"] == "22370"
        assert fields["LIGHTER_API_KEY_INDEX"]["value"] == "4"
        saved = env.read_text(encoding="utf-8")
        assert "UNRELATED=keep-me" in saved
        assert "DRY_RUN=true" in saved
        assert "DRY_RUN=false" not in saved
        assert f"ARCUS_ADDRESS={ADDRESS}" in saved
        assert f"ARCUS_API_PRIVATE_KEY={PRIVATE}" in saved
        assert f"LIGHTER_API_PRIVATE_KEY={LIGHTER_PRIVATE}" in saved
        assert f"ARCUS_API_KEY={API_KEY}" in saved
        assert "ARCUS_ACCOUNT_INDEX=2" in saved
        assert "LIGHTER_ACCOUNT_INDEX=22370" in saved
        assert "LIGHTER_API_KEY_INDEX=4" in saved
        assert "ARCUS_API_PRIVATE_KEY_FILE=keys/unit-test-private.pem" in saved
        assert 'ARCUS_PROXY="socks5://unit-user:p#ss@10.1.2.3:1080"' in saved
        # 重新从磁盘读，和运行中的对象是同一套值，# 没有被 dotenv 截掉
        reloaded = Settings.load(env, app.state.settings.data_dir)
        assert reloaded.arcus_api_private_key == PRIVATE
        assert reloaded.lighter_api_private_key == LIGHTER_PRIVATE
        assert reloaded.arcus_proxy == PROXY
        assert reloaded.arcus_address == ADDRESS
        assert reloaded.dry_run is True
        again = run(endpoint(app, "GET", "/api/account-settings")())
        assert_redacted(again, PRIVATE, LIGHTER_PRIVATE, API_KEY, ADDRESS, PROXY, "p#ss")
        status = run(endpoint(app, "GET", "/api/status")())
        tasks = run(endpoint(app, "GET", "/api/tasks")())
        assert_redacted(status, PRIVATE, LIGHTER_PRIVATE, API_KEY, ADDRESS, PROXY)
        assert_redacted(tasks, PRIVATE, LIGHTER_PRIVATE, API_KEY, ADDRESS, PROXY)
        app.state.store.close()


def test_blank_secret_keeps_the_saved_value_and_other_fields_can_change():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        env = app.state.settings.env_path
        env.write_text("DRY_RUN=true\n", encoding="utf-8")
        save = endpoint(app, "POST", "/api/account-settings")
        run(save({
            "ARCUS_API_PRIVATE_KEY": PRIVATE,
            "ARCUS_ADDRESS": ADDRESS,
            "LIGHTER_API_PRIVATE_KEY": LIGHTER_PRIVATE,
            "LIGHTER_API_KEY_INDEX": "1",
            "ARCUS_PROXY": PROXY,
        }))
        app.state.service.client.rebinds = 0
        body = run(save({
            "ARCUS_API_PRIVATE_KEY": "",
            "ARCUS_API_PRIVATE_KEY_FILE": "",
            "LIGHTER_API_PRIVATE_KEY": "****",
            "ARCUS_ADDRESS": "",
            "ARCUS_PROXY": "****",
            "LIGHTER_API_KEY_INDEX": "6",
            "ARCUS_ACCOUNT_INDEX": "3",
        }))
        settings = app.state.settings
        assert settings.arcus_api_private_key == PRIVATE
        assert settings.lighter_api_private_key == LIGHTER_PRIVATE
        assert settings.arcus_address == ADDRESS
        assert settings.arcus_proxy == PROXY
        assert settings.lighter_api_key_index == 6
        assert settings.arcus_account_index == 3
        assert app.state.service.client.rebinds == 0
        saved = env.read_text(encoding="utf-8")
        assert f"ARCUS_API_PRIVATE_KEY={PRIVATE}" in saved
        assert f"LIGHTER_API_PRIVATE_KEY={LIGHTER_PRIVATE}" in saved
        assert f"ARCUS_ADDRESS={ADDRESS}" in saved
        assert "LIGHTER_API_KEY_INDEX=6" in saved
        assert "ARCUS_ACCOUNT_INDEX=3" in saved
        assert_redacted(body, PRIVATE, LIGHTER_PRIVATE, ADDRESS, PROXY, "p#ss")
        masked = "0xe24d…7777"
        status, detail = http_error(save({"ARCUS_ADDRESS": masked}))
        assert status == 400
        assert ADDRESS not in detail and masked not in detail or "打码" in detail
        assert settings.arcus_address == ADDRESS
        assert f"ARCUS_ADDRESS={ADDRESS}" in env.read_text(encoding="utf-8")
        app.state.store.close()


def test_invalid_values_do_not_touch_the_file_or_the_running_settings():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        env = app.state.settings.env_path
        env.write_text("DRY_RUN=true\nARCUS_ACCOUNT_INDEX=1\n", encoding="utf-8")
        app.state.settings.arcus_account_index = 1
        save = endpoint(app, "POST", "/api/account-settings")
        before = env.read_text(encoding="utf-8")
        status, detail = http_error(save({"ARCUS_ADDRESS": "0x1234", "ARCUS_ACCOUNT_INDEX": "8"}))
        assert status == 400 and "40" in detail
        assert "0x1234" not in detail
        assert env.read_text(encoding="utf-8") == before
        assert app.state.settings.arcus_account_index == 1
        status, detail = http_error(save({"ARCUS_ACCOUNT_INDEX": "10"}))
        assert status == 400 and "0~9" in detail
        status, detail = http_error(save({"ARCUS_API_KEY": "zzzz"}))
        assert status == 400
        assert "zzzz" not in detail
        status, detail = http_error(save({"ARCUS_API_PRIVATE_KEY": PRIVATE + "\nMORE"}))
        assert status == 400
        assert PRIVATE not in detail
        status, detail = http_error(save({"LIGHTER_API_KEY_INDEX": "-3"}))
        assert status == 400
        assert env.read_text(encoding="utf-8") == before
        # 清空可见的 Lighter 索引会取消；密钥留空不动
        body = run(save({
            "LIGHTER_ACCOUNT_INDEX": "15",
            "LIGHTER_API_KEY_INDEX": "2",
            "LIGHTER_API_PRIVATE_KEY": LIGHTER_PRIVATE,
        }))
        assert field_map(body)["LIGHTER_ACCOUNT_INDEX"]["value"] == "15"
        body = run(save({
            "LIGHTER_ACCOUNT_INDEX": "",
            "LIGHTER_API_KEY_INDEX": "",
            "LIGHTER_API_PRIVATE_KEY": "",
        }))
        assert app.state.settings.lighter_account_index is None
        assert app.state.settings.lighter_api_key_index is None
        assert app.state.settings.lighter_api_private_key == LIGHTER_PRIVATE
        assert app.state.reconciler.account_index is None
        fields = field_map(body)
        assert fields["LIGHTER_ACCOUNT_INDEX"]["value"] == ""
        assert fields["LIGHTER_API_PRIVATE_KEY"]["display"] == "****"
        assert_redacted(body, LIGHTER_PRIVATE)
        reloaded = Settings.load(env, app.state.settings.data_dir)
        assert reloaded.lighter_account_index is None
        assert reloaded.lighter_api_private_key == LIGHTER_PRIVATE
        app.state.store.close()


def test_missing_env_file_is_created_beside_the_app_and_panel_has_the_tab():
    with tempfile.TemporaryDirectory() as tmp:
        app = make_app(tmp)
        env = app.state.settings.env_path
        assert not env.exists()
        run(endpoint(app, "POST", "/api/account-settings")({
            "LIGHTER_API_KEY_INDEX": "3",
            "ARCUS_API_URL": "https://api.arcus.xyz/",
        }))
        assert env.is_file()
        text = env.read_text(encoding="utf-8")
        assert "LIGHTER_API_KEY_INDEX=3" in text
        assert "ARCUS_API_URL=https://api.arcus.xyz" in text
        assert app.state.settings.arcus_api_url == "https://api.arcus.xyz"
        app.state.store.close()
    root = Path(__file__).resolve().parents[1]
    html = (root / "src/parallax_hedge/web/index.html").read_text(encoding="utf-8")
    script = (root / "src/parallax_hedge/web/app.js").read_text(encoding="utf-8")
    assert "资金费总览" in html and "轮换任务" in html and "系统" in html
    assert 'data-page="account"' in html and "账户设置" in html
    assert "const value = masked ? ''" in script
    assert "/api/account-settings" in script
