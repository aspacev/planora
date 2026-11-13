import os
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Extracted into a name because it is compared against below: "the value is still
# the default" is the only available sign that nobody set PUBLIC_BASE_URL, and
# guessing a domain over one set by hand is not allowed.
_LOCAL_BASE_URL = "http://localhost:8000"

#: Registration modes. `open` — anyone, `invite_only` — by invitation only,
#: `closed` — sign-in exists, registration does not.
SIGNUP_MODES = ("open", "invite_only", "closed")

# What must be set for each mail transport. Transports with no requirements are
# absent from the list: disabled mail and writing to the log have no requirements
# by definition.
_MAIL_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "smtp": ("smtp_url", "mail_from"),
    "api": ("mail_api_url", "mail_api_key", "mail_from"),
}
#: The mail transports this installation can do. `none` — there are no messages at
#: all, and the interface does not show the send button; `log` — the same writing
#: to the log, but the installation counts as mail-capable (the button is there,
#: the message is found in the log) — the difference is needed in development.
MAIL_TRANSPORTS: tuple[str, ...] = ("none", "log", *_MAIL_REQUIREMENTS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_REPO_ROOT / ".env", extra="ignore")

    database_url: str
    app_secret: str
    #: The one address that holds the director role — the /admin panel and
    #: whatever is ever added to it. Mandatory and with no default value: the
    #: director role must not fall to the first person who forgot to configure it.
    #: Not a role within an organization (Role.OWNER only makes sense inside one
    #: organization, and an installation may have any number of organization
    #: owners) — a property of the installation itself.
    director_email: str

    public_base_url: str = _LOCAL_BASE_URL
    #: The Secure flag of the session cookie. None means derive it automatically:
    #: from the incoming request's scheme (behind a proxy, from
    #: X-Forwarded-Proto) or from PUBLIC_BASE_URL. An explicit value is for
    #: installations where the automatic choice gets it wrong: for example, TLS is
    #: terminated ahead of a proxy that does not pass X-Forwarded-Proto on.
    cookie_secure: bool | None = None
    default_locale: str = "az"
    supported_locales: str = "az,en,ru"
    signup_mode: str = "open"

    mail_transport: str = "none"
    smtp_url: str = ""
    mail_api_key: str = ""
    mail_api_url: str = ""
    mail_from: str = ""
    invite_ttl_days: int = 7
    invite_rate_limit: int = 20

    #: How long an exported document stays valid, in days from the export date:
    #: the export date is the date it is sent to the orderer, and the "valid
    #: until" on the cover page is counted from it. An installation setting rather
    #: than a field in the export form: while the term is the same for everyone, a
    #: field would only get in the way — once someone asks for otherwise, we will
    #: add one.
    export_validity_days: int = 30

    public_sharing_enabled: bool = True
    guest_comment_rate_limit: int = 10

    # The sign-in and registration limits (0 disables the corresponding limit).
    # By IP every attempt is counted, by account only the failed ones: a successful
    # sign-in from two devices must not lock a person out, whereas a dozen wrong
    # passwords for one address is guessing, whoever it comes from.
    login_rate_limit_per_ip: int = 30
    login_rate_limit_per_account: int = 10
    signup_rate_limit_per_ip: int = 10
    # Password recovery requests are counted by IP and all of them: each one is a
    # message to an arbitrary address entered into a form without signing in.
    password_reset_rate_limit_per_ip: int = 10
    auth_rate_window_seconds: int = 900

    # The AI limits: model requests per minute and tokens per day — per
    # organization (0 means no limit). The budget guards the key owner's money:
    # without it one member can burn the key's monthly limit in an evening.
    ai_requests_per_minute: int = 10
    ai_daily_token_budget: int = 200_000

    ai_max_questions: int = 12
    ai_schema_retries: int = 2
    ai_request_timeout: int = 60
    #: Allow an LLM address with the http scheme and in private ranges. Forbidden
    #: by default (SSRF protection: the address is set by a user while the server
    #: is what goes there); true is a deliberate choice for a self-hosted
    #: installation with a local model on its own network.
    ai_allow_private_urls: bool = False

    max_tasks_per_project: int = 2000
    max_text_len: int = 4000
    #: The ceiling on a request body's size in bytes. An order of magnitude above
    #: the largest lawful body (an AI draft of hundreds of tasks is tens of
    #: kilobytes); anything beyond is not a form but a flood.
    max_body_bytes: int = 1_000_000
    log_level: str = "INFO"

    #: Whether the live feed (WebSocket) works on this installation. None means
    #: derive it: on Vercel there are no sockets (serverless cuts the upgrade off),
    #: in other layouts there are. The client reads the flag from /api/config and
    #: does not spend connection attempts where there is nothing to apply them to.
    live_enabled: bool | None = None

    @model_validator(mode="after")
    def _resolve_live_enabled(self) -> Self:
        if self.live_enabled is None:
            self.live_enabled = os.getenv("VERCEL") is None
        return self

    @field_validator("app_secret")
    @classmethod
    def _refuse_a_secret_that_is_not_one(cls, value: str) -> str:
        """Refuses to start with a placeholder secret or a stub.

        APP_SECRET signs sessions and encrypts LLM keys. The value from
        .env.example left as it is means that any installation's cookie can be
        forged by anyone who has read the repository — and that must be a refusal
        to start rather than a silent hole. A short secret is the same hole in
        profile: it gets brute-forced.

        Rotating the secret is not a free operation: stored LLM keys are encrypted
        with the previous value and will not decrypt after a change (see
        app/crypto.py) — they will have to be entered again.
        """
        if value == "change-me-to-a-long-random-string":
            raise ValueError(
                "APP_SECRET остался значением из .env.example — задай свой: "
                "openssl rand -hex 32"
            )
        if len(value) < 16:
            raise ValueError("APP_SECRET короче 16 символов — задай длиннее: openssl rand -hex 32")
        return value

    @field_validator("director_email")
    @classmethod
    def _refuse_a_director_email_that_is_not_one(cls, value: str) -> str:
        """Refuses to start with a placeholder address or with nothing.

        DIRECTOR_EMAIL decides who sees the director's panel (/admin). The value
        from .env.example left as it is means the director role would fall to an
        address present in every copy of the repository — that must be a refusal to
        start rather than a silent hole. An empty value is the same hole in
        reverse: the panel is visible to nobody at all.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError(
                "DIRECTOR_EMAIL не задан — без него роль директора не достаётся никому."
            )
        if stripped == "change-me-to-your-email@example.com":
            raise ValueError(
                "DIRECTOR_EMAIL остался значением из .env.example — задай свой адрес."
            )
        return stripped

    @field_validator("database_url")
    @classmethod
    def _spell_out_the_driver(cls, value: str) -> str:
        """Appends the driver to the database URL if it is not there.

        Managed databases (Neon, Supabase, Vercel Marketplace) hand out a
        connection string as `postgresql://...`, and some — with a Heroku legacy —
        as `postgres://...`. On the first, SQLAlchemy goes looking for psycopg2,
        which is not among the dependencies, and on the second it simply refuses to
        parse the URL. This used to be fixed by hand every time the string was
        copied from a dashboard — and broke silently when an integration wrote the
        variable itself and there was nothing to edit.

        A driver that is already set is left alone: `postgresql+psycopg` is already
        right, and `postgresql+asyncpg` is a deliberate choice by whoever wrote it.
        """
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return f"postgresql+psycopg://{value[len(prefix):]}"
        return value

    @field_validator("signup_mode")
    @classmethod
    def _reject_a_value_nobody_implements(cls, value: str) -> str:
        """Rejects an unknown switch value at start-up rather than on the first use
        of it.

        Otherwise a typo in `SIGNUP_MODE` silently turns the installation into a
        closed one (the comparison with `open` does not match) — a refusal
        indistinguishable from the intended behaviour and therefore hunted for
        hours; a refusal to start is found in a second. The same thing about
        `MAIL_TRANSPORT` is checked below, together with the variables the
        transport requires.
        """
        if value not in SIGNUP_MODES:
            raise ValueError(f"допустимые значения: {', '.join(SIGNUP_MODES)}")
        return value

    @model_validator(mode="after")
    def _borrow_the_domain_from_the_platform(self) -> Self:
        """Derives PUBLIC_BASE_URL from the domain the platform handed out.

        On Vercel the domain is known only after the first deploy, so the variable
        cannot be set in advance: it comes out as a circle — first the deploy, then
        the value, then the deploy again. And until the second deploy the session
        cookie rides out over https without the Secure flag, because that flag is
        derived from here (see app.api.auth_routes).

        VERCEL_PROJECT_PRODUCTION_URL is the project's permanent domain, VERCEL_URL
        is the address of a specific deploy; the platform sets both itself, both
        without a scheme. The first is prettier and survives a redeploy, the second
        is always there — hence the order.

        A value that is set takes precedence: a custom domain attached to the
        project is not shown by the platform in these variables.
        """
        if self.public_base_url != _LOCAL_BASE_URL:
            return self

        host = os.getenv("VERCEL_PROJECT_PRODUCTION_URL") or os.getenv("VERCEL_URL")
        if host:
            self.public_base_url = f"https://{host}"
        return self

    @model_validator(mode="after")
    def _refuse_a_mail_setup_that_cannot_send(self) -> Self:
        """Does not let the application start with mail configured halfway.

        Without this check, `MAIL_TRANSPORT=smtp` with an empty `SMTP_URL` would
        surface only on the first message — that is, on the registration of the
        very first user, and silently: the message did not go out, and there is a
        line in a log nobody reads. A typo in the value itself
        (`MAIL_TRANSPORT=stmp`) must all the more not quietly turn into "mail is
        off".

        Disabled mail requires no checks: an installation without a mail server is
        a lawful deployment option rather than an under-configured one.
        """
        if self.mail_transport not in MAIL_TRANSPORTS:
            raise ValueError(
                f"MAIL_TRANSPORT={self.mail_transport!r}: допустимы "
                f"{', '.join(MAIL_TRANSPORTS)}"
            )

        missing = [
            name.upper()
            for name in _MAIL_REQUIREMENTS.get(self.mail_transport, ())
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError(
                f"MAIL_TRANSPORT={self.mail_transport}, но не задано: {', '.join(missing)}"
            )
        return self

    @property
    def mail_enabled(self) -> bool:
        """Whether there is anywhere to send messages.

        With `none` the interface does not show the send button at all — copying
        the link remains, while the message itself goes to the log. `log` writes to
        the same place, but the installation counts as mail-capable: in development
        the button has to be there and the message is read in the log.
        """
        return self.mail_transport != "none"

    @property
    def locales(self) -> list[str]:
        return [item.strip() for item in self.supported_locales.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
