import os

os.environ.setdefault("APP_SECRET", "test-secret-not-for-production")
# Mandatory under the very same validation as APP_SECRET (see app.config). Tests
# that care about a particular director address substitute the variable themselves
# with monkeypatch and reset the settings cache — see tests/test_admin_api.py.
os.environ.setdefault("DIRECTOR_EMAIL", "director@example.com")
# The test LLM addresses are made-up hosts and localhost; the address publicity
# check (SSRF protection, app/ai/netguard.py) would resolve them over the network
# on every test. The check itself is tested separately, with the switch explicitly
# off and the resolver substituted — without a single real DNS query.
os.environ.setdefault("AI_ALLOW_PRIVATE_URLS", "true")
# The AI rate and budget limits are off in the general run: the end-to-end
# interview scenario makes more model calls per second than a person is allowed per
# minute. The gates themselves are checked by separate tests that enable the limit
# on a specific settings object.
os.environ.setdefault("AI_REQUESTS_PER_MINUTE", "0")
os.environ.setdefault("AI_DAILY_TOKEN_BUDGET", "0")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db import Base


def _safe_test_database_url() -> str:
    """The test database URL, derived from DATABASE_URL rather than the production URL as is.

    Destructive operations (drop_all/create_all) are allowed only against a database
    whose name ends in "_test", and never against the one from DATABASE_URL —
    otherwise a test run would one day wipe the working development database. The
    URL is parsed through make_url()/set() rather than by string concatenation so
    that it does not fall apart on passwords with special characters (slashes, for
    instance).
    """
    prod_url = make_url(get_settings().database_url)
    prod_db = prod_url.database
    test_db = f"{prod_db}_test"
    test_url = prod_url.set(database=test_db)

    if test_db == prod_db or not test_db.endswith("_test"):
        raise RuntimeError(
            "отказ выполнять drop_all/create_all: выведенное имя тестовой базы "
            f"{test_db!r} совпадает с боевым DATABASE_URL ({prod_db!r}) или не "
            "оканчивается на '_test'. Тесты никогда не должны трогать базу из "
            "DATABASE_URL."
        )
    # render_as_string(hide_password=False): str(url) masks the password with
    # asterisks, while we need the real DSN to connect.
    return test_url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def engine():
    test_url = _safe_test_database_url()
    engine = create_engine(test_url)

    try:
        with engine.connect():
            pass
    except OperationalError as exc:
        url = make_url(test_url)
        raise RuntimeError(
            f"тестовая база {url.database!r} недоступна "
            f"({url.render_as_string(hide_password=True)}).\n"
            "Если её ещё нет, создай её — Postgres поднят через docker compose, "
            "сервис 'db':\n"
            f"  docker compose exec db createdb -U {url.username} {url.database}\n"
            "или, если Postgres не в докере:\n"
            f"  createdb -h {url.host} -p {url.port} -U {url.username} {url.database}\n"
            f"Исходная ошибка: {exc}"
        ) from exc

    # The whole schema rather than Base.metadata.drop_all: that drops only the
    # tables the models still describe. A table whose model has been removed
    # stays behind in a test database left over from an earlier run, and its
    # foreign keys then forbid dropping the tables it points at.
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def mailbox(monkeypatch):
    """Intercepts messages instead of sending them and returns the list delivered.

    build_transport is substituted rather than app.mail.send: that way both
    assembling the text from the template and choosing the recipient's language pass
    through the test — that is, exactly what breaks when the dictionaries are edited
    and rides silently out in a sent message.
    """
    import app.mail as mail_module

    delivered: list[mail_module.Letter] = []

    class Recorder:
        def deliver(self, letter: mail_module.Letter) -> None:
            delivered.append(letter)

    monkeypatch.setattr(mail_module, "build_transport", lambda settings: Recorder())
    return delivered


@pytest.fixture
def db(engine):
    """A session inside a transaction that is rolled back after the test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection)()
    yield session
    session.close()
    transaction.rollback()
    connection.close()
