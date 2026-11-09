"""The setup wizard's logic with a scripted terminal, against the token and Helix fakes: the token checked live and
never shown, scopes per feature, channels and the owner, who may ask, and what gets saved."""

from __future__ import annotations

import pytest

from harness import TOKEN
from tallyhand import wizard
from tallyhand.wizard import Endpoints, Store


class Script:
    """A terminal that answers from a list and keeps everything the wizard printed."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.out: list[str] = []
        self.secrets: list[str] = []

    def _next(self, question: str):
        self.out.append(f"? {question}")
        assert self.answers, f"the wizard asked more than the script answers: {question}"
        return self.answers.pop(0)

    def text(self, question: str, default: str = "") -> str:
        answer = self._next(question)
        return default if answer is None else answer

    def secret(self, question: str) -> str:
        answer = self._next(question)
        self.secrets.append(answer)
        return answer

    def choice(self, question: str, options: list[str], default: int = 0) -> int:
        self.out.extend(f"  {index}. {option}" for index, option in enumerate(options))
        return self._next(question)

    def yes_no(self, question: str, default: bool = True) -> bool:
        return self._next(question)

    def info(self, text: str) -> None:
        self.out.append(text)

    def warn(self, text: str) -> None:
        self.out.append(f"! {text}")

    @property
    def printed(self) -> str:
        return "\n".join(self.out)


class Saved:
    def __init__(self, env: dict | None = None):
        self.env = dict(env or {})
        self.applied = 0

    def store(self) -> Store:
        def apply():
            self.applied += 1
            return True, True

        return Store(read_env=lambda name: self.env.get(name, ""), save_env=self.env.__setitem__, apply_platform=apply)


@pytest.fixture
def twitch_api(helix_api):
    helix_api.add_user("chan", "2000", "Chan")
    helix_api.add_user("friend", "3000", "Friend")
    helix_api.add_user("alice", "42", "Alice")
    helix_api.add_user("mo", "47", "Mo")
    return helix_api


@pytest.fixture
def endpoints(twitch_api) -> Endpoints:
    return Endpoints(validate_url=twitch_api.validate.url, helix_url=twitch_api.url)


def test_a_full_run_with_owner_only(endpoints):
    script, saved = Script(f"oauth:{TOKEN}", "Chan, #friend", wizard.OWNER_ONLY, True), Saved()
    assert wizard.run(script, saved.store(), endpoints) is True
    assert saved.env == {"TWITCH_BOT_TOKEN": TOKEN, "TWITCH_CHANNEL": "chan,friend", "TWITCH_ALLOWED_USERS": "2000",
                         "TWITCH_ALLOW_ALL_USERS": "false"}
    assert saved.applied == 1
    assert TOKEN not in script.printed
    assert "The token belongs to tallybot (user id 1000)." in script.printed
    assert "It expires in 57d 20h. Tallyhand never refreshes it" in script.printed
    assert "  Clips: !clip, twitch_create_clip, automatic clips: ready" in script.printed
    assert "The owner is Chan (chan, user id 2000), the broadcaster of #chan" in script.printed
    assert "Saved as Twitch user ids: chan 2000." in script.printed
    assert "platform_toolsets.twitch: [todo] and known_plugin_toolsets.twitch: [obs, twitch]." in script.printed
    assert "hermes gateway restart" in script.printed


def test_a_token_without_an_expiry_says_so(endpoints, twitch_api):
    twitch_api.validate.respond(expires_in=0)
    script, saved = Script(TOKEN, "chan", wizard.OWNER_ONLY, False), Saved()
    assert wizard.run(script, saved.store(), endpoints)
    assert "Twitch reported no expiry for it." in script.printed


def test_missing_scopes_are_named_per_feature(endpoints, twitch_api):
    twitch_api.validate.respond(scopes=["chat:read", "chat:edit", "clips:edit"])
    script, saved = Script(TOKEN, "chan", wizard.OWNER_ONLY, False), Saved()
    assert wizard.run(script, saved.store(), endpoints)
    assert "  Timeouts, bans and unbans: needs moderator:manage:banned_users" in script.printed
    assert "  Chat: ready" in script.printed and saved.applied == 0


def test_a_token_without_chat_scopes_saves_nothing(endpoints, twitch_api):
    twitch_api.validate.respond(scopes=["clips:edit"])
    script, saved = Script(TOKEN, TOKEN, TOKEN), Saved()
    assert wizard.run(script, saved.store(), endpoints) is False
    assert saved.env == {} and "Nothing was saved" in script.printed


def test_a_rejected_token_is_asked_again(endpoints, twitch_api):
    twitch_api.validate.respond(status=401)
    script, saved = Script("badtoken", "", "badtoken"), Saved()
    assert wizard.run(script, saved.store(), endpoints) is False
    assert script.printed.count("Twitch did not accept the token") == 2 and "A token is required" in script.printed
    assert saved.env == {} and "badtoken" not in script.printed


def test_enter_keeps_the_token_already_set(endpoints):
    script, saved = Script("", "chan", wizard.OWNER_ONLY, False), Saved({"TWITCH_BOT_TOKEN": f"oauth:{TOKEN}"})
    assert wizard.run(script, saved.store(), endpoints)
    assert saved.env["TWITCH_BOT_TOKEN"] == TOKEN
    assert "Enter keeps the one already set" in script.printed


def test_unknown_channels_are_asked_again(endpoints):
    script, saved = Script(TOKEN, "chan,nosuchchannel", "", "chan", wizard.OWNER_ONLY, False), Saved()
    assert wizard.run(script, saved.store(), endpoints)
    assert "Twitch has no channel named nosuchchannel." in script.printed
    assert "Give at least one channel login" in script.printed
    assert saved.env["TWITCH_CHANNEL"] == "chan"


def test_enter_keeps_the_channels_already_set(endpoints):
    script = Script(TOKEN, None, wizard.OWNER_ONLY, False)
    saved = Saved({"TWITCH_CHANNEL": "#Friend, chan"})
    assert wizard.run(script, saved.store(), endpoints)
    assert saved.env["TWITCH_CHANNEL"] == "friend,chan" and saved.env["TWITCH_ALLOWED_USERS"] == "3000"


def test_a_list_of_logins(endpoints):
    script = Script(TOKEN, "chan", wizard.LIST, "Alice, nobody", "@Alice, mo, chan", False)
    saved = Saved()
    assert wizard.run(script, saved.store(), endpoints)
    assert "Twitch has no account named nobody." in script.printed
    assert (saved.env["TWITCH_ALLOWED_USERS"], saved.env["TWITCH_ALLOW_ALL_USERS"]) == ("2000,42,47", "false")
    assert "Saved as Twitch user ids: chan 2000, alice 42, mo 47." in script.printed


def test_everyone_needs_a_yes_after_the_warning(endpoints):
    script, saved = Script(TOKEN, "chan", wizard.EVERYONE, True, False), Saved()
    assert wizard.run(script, saved.store(), endpoints)
    assert (saved.env["TWITCH_ALLOWED_USERS"], saved.env["TWITCH_ALLOW_ALL_USERS"]) == ("", "true")
    assert "every question costs you a model call" in script.printed
    script, saved = Script(TOKEN, "chan", wizard.EVERYONE, False, False), Saved()
    assert wizard.run(script, saved.store(), endpoints)
    assert (saved.env["TWITCH_ALLOWED_USERS"], saved.env["TWITCH_ALLOW_ALL_USERS"]) == ("2000", "false")


def test_a_twitch_outage_saves_nothing(endpoints, twitch_api):
    twitch_api.force(503, times=10)
    script, saved = Script(TOKEN, "chan"), Saved()
    assert wizard.run(script, saved.store(), endpoints) is False
    assert "Twitch did not answer an account lookup" in script.printed and "Nothing was saved" in script.printed
    assert saved.env == {} and TOKEN not in script.printed


def test_a_toolset_already_chosen_is_named_as_kept(endpoints):
    script, saved = Script(TOKEN, "chan", wizard.OWNER_ONLY, True), Saved()
    store = saved.store()
    store.apply_platform = lambda: (False, True)
    assert wizard.run(script, store, endpoints)
    assert "skip_context_files: true and known_plugin_toolsets.twitch: [obs, twitch]." in script.printed
    assert "platform_toolsets.twitch was already set and stays as it is." in script.printed


def test_hermes_default_toolsets_leave_the_known_list_alone(endpoints):
    script, saved = Script(TOKEN, "chan", wizard.OWNER_ONLY, True), Saved()
    store = saved.store()
    store.apply_platform = lambda: (False, False)
    assert wizard.run(script, store, endpoints)
    assert "known_plugin_toolsets.twitch was left alone" in script.printed


# through hermes gateway setup


def test_hermes_gateway_setup_runs_the_wizard_and_saves_through_hermes(registered_twitch, twitch_api, monkeypatch,
                                                                         tmp_path, capsys):
    """Hermes' own menu entry (gateway_setup_wizard._configure_platform) runs setup_fn; the answers land in the
    profile's .env through save_env_value and the Twitch settings in config.yaml where the gateway reads them."""
    from types import SimpleNamespace

    from gateway.session_execution import GatewaySessionAgentMixin
    from hermes_cli.config import load_config_readonly
    from hermes_cli.gateway_setup_wizard import _configure_platform

    from tallyhand import auth, helix

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("# mine\nplatform_toolsets:\n  cli: [web]\ngateway:\n  platforms:\n"
                                      "    discord:\n      skip_context_files: false\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(auth, "VALIDATE_URL", twitch_api.validate.url)
    monkeypatch.setattr(helix, "BASE_URL", twitch_api.url)
    answers = iter([f"oauth:{TOKEN}", "chan"])
    monkeypatch.setattr("hermes_cli.setup.prompt", lambda question, default=None, password=False: next(answers))
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda question, choices, default=0, description=None: 0)
    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", lambda question, default=True, description=None: True)
    entry = registered_twitch()
    _configure_platform({"key": "twitch", "label": "Twitch", "emoji": entry.emoji, "token_var": "TWITCH_BOT_TOKEN",
                         "install_hint": entry.install_hint, "_registry_entry": entry})
    env = (home / ".env").read_text(encoding="utf-8")
    assert f"TWITCH_BOT_TOKEN={TOKEN}" in env and "TWITCH_CHANNEL=chan" in env
    assert "TWITCH_ALLOWED_USERS=2000" in env and "TWITCH_ALLOW_ALL_USERS=false" in env
    assert (home / ".env").stat().st_mode & 0o777 == 0o600
    assert TOKEN not in capsys.readouterr().out
    config = load_config_readonly()
    reader = SimpleNamespace(_runner=None, _ctx=SimpleNamespace(source=None, user_config=config))
    assert GatewaySessionAgentMixin._skip_context_files(reader, "twitch") is True  # where the gateway reads it
    assert config["platform_toolsets"] == {"cli": ["web"], "twitch": ["todo"]}
    assert config["known_plugin_toolsets"] == {"twitch": ["obs", "twitch"]}
    assert config["gateway"]["platforms"]["discord"] == {"skip_context_files": False}
    assert (home / "config.yaml").read_text(encoding="utf-8").startswith("# mine")


@pytest.mark.parametrize(("saved", "known", "applied", "toolsets", "known_after"), [
    ("platform_toolsets:\n  twitch: [todo, web]\n", "", (False, True), ["todo", "web"], ["obs", "twitch"]),
    ("platform_toolsets:\n  twitch: [todo]\n", "known_plugin_toolsets:\n  twitch: [a2a]\n", (False, True), ["todo"],
     ["a2a", "obs", "twitch"]),
    # Hermes' default composite: a known list would take the twitch and obs tools from the owner's own turns.
    ("platform_toolsets:\n  twitch: [hermes-twitch]\n", "", (False, False), ["hermes-twitch"], None),
])
def test_the_wizard_keeps_a_toolset_already_chosen(monkeypatch, tmp_path, saved, known, applied, toolsets,
                                                   known_after):
    from hermes_cli.config import load_config_readonly

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(saved + known, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert wizard.apply_platform() == applied
    config = load_config_readonly()
    assert config["platform_toolsets"]["twitch"] == toolsets
    assert (config.get("known_plugin_toolsets") or {}).get("twitch") == known_after
    assert config["gateway"]["platforms"]["twitch"]["skip_context_files"] is True


def test_declining_to_reconfigure_changes_nothing(monkeypatch, capsys):
    asked = []

    def confirm(label, *env_vars):  # Hermes' confirm_reconfigure: False when configured and the owner says no
        asked.append((label, env_vars))
        return False

    monkeypatch.setattr("hermes_cli.gateway_setup_wizard.confirm_reconfigure", confirm)
    monkeypatch.setattr(wizard, "run", lambda *a, **k: pytest.fail("the wizard ran after the owner said no"))
    wizard.setup()
    assert asked == [("Twitch", ("TWITCH_BOT_TOKEN", "TWITCH_CHANNEL"))]
    assert "Twitch Setup" in capsys.readouterr().out
