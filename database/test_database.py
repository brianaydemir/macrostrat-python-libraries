# Skeletal testing file
from pathlib import Path
from pytest import fixture
from dotenv import load_dotenv
from psycopg2.sql import SQL, Identifier, Literal, Placeholder
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.sql import text

from macrostrat.utils import relative_path, get_logger
from macrostrat.database import Database, run_sql
from macrostrat.database.utils import temp_database, infer_is_sql_text
from macrostrat.database.postgresql import table_exists
from pytest import warns, raises, mark
from os import environ

load_dotenv()

log = get_logger(__name__)


@fixture(scope="session")
def engine(database_url, pytestconfig):
    with temp_database(database_url, drop=pytestconfig.option.teardown) as engine:
        yield engine


@fixture(scope="session")
def db(engine):
    return Database(engine.url)


@fixture(scope="function")
def conn(db):
    """A connection managed by the database session."""
    connection = db.session.connection()
    yield connection
    db.session.rollback()


def test_database(db):
    # Get schema files
    schema_files = Path(relative_path(__file__, "test-fixtures")).glob("*.sql")

    file_list = list(schema_files)
    assert len(file_list) == 1

    # Create tables
    for sqlfile in file_list:
        res = run_sql(db.engine, sqlfile)
        assert len(res) == 3

    db.automap(schemas=["public", "geology"])

    # Test that tables exist
    assert "sample" in db.model
    assert "geology_formation" in db.model


def test_database_mapper(db):
    Sample = db.model.sample
    Formation = db.model.geology_formation

    assert Sample.__table__.name == "sample"

    s = Sample(name="Test")
    f = Formation(name="Test")
    s._formation = f

    assert isinstance(s._formation, Formation)


def test_sql_text_inference():
    assert infer_is_sql_text("SELECT * FROM sample")


def test_sql_text_inference_2():
    assert infer_is_sql_text(b"SELECT * FROM sample")


def test_sql_text_inference_3():
    assert not infer_is_sql_text("sample.sql")


def test_sql_interpolation_psycopg(db):
    sql = "INSERT INTO sample (name) VALUES (:name)"
    assert infer_is_sql_text(sql)

    # db.engine.execute(sql, name="Test")
    db.run_sql(sql, params=dict(name="Test"), raise_errors=True)
    db.session.commit()

    sql1 = "SELECT * FROM sample WHERE name = :name"
    res = list(db.run_sql(sql1, params=dict(name="Test"), raise_errors=True))[0]
    assert res.first().name == "Test"


def test_extraneous_argument(db):
    sql = "INSERT INTO sample (name) VALUES (:name)"
    assert infer_is_sql_text(sql)

    # db.engine.execute(sql, name="Test")
    db.run_sql(sql, params=dict(name="Test2", extraneous="TestA"))


def test_sql_identifier(db):
    sql = (
        SQL("SELECT name FROM {table} WHERE name = {name}")
        .format(table=Identifier("sample"), name=Literal("Test"))
        .as_string(db.engine.raw_connection().cursor())
    )
    assert infer_is_sql_text(sql)
    res = list(db.run_sql(sql, raise_errors=True))
    assert len(res) == 1
    assert res[0].scalar() == "Test"


def test_raises_deprecation(db):
    sql = (
        SQL("SELECT name FROM {table} WHERE name = {name}")
        .format(table=Identifier("sample"), name=Literal("Test"))
        .as_string(db.engine.raw_connection().cursor())
    )
    with warns(DeprecationWarning):
        db.run_sql(sql, stop_on_error=True)


def test_partial_identifier(db):
    """https://www.postgresql.org/docs/current/sql-prepare.html"""
    conn = db.engine.raw_connection()
    cursor = conn.cursor()
    sql = (
        SQL("SELECT name FROM sam{partial_table} WHERE name = {name}")
        .format(name=Placeholder("name"), partial_table=SQL("ple"))
        .as_string(cursor)
    )

    with db.engine.begin() as conn:
        res = conn.exec_driver_sql(sql, dict(name="Test")).scalar()
        assert res == "Test"


def test_deprecated_keyword(db):
    sql1 = "SELECT * FROM sample WHERE name = :name"
    # Check that it raises the appropriate warning
    with warns(DeprecationWarning):
        db.run_sql(sql1, params=dict(name="Test"), stop_on_error=True)


def test_query_error(db):
    sql1 = "SELECT * FROM samplea WHERE name = :name"
    with raises(ProgrammingError), warns(DeprecationWarning):
        db.run_sql(sql1, params=dict(name="Test"), stop_on_error=True)


def test_query_error_1(db):
    sql1 = "SELECT * FROM samplea WHERE name = :name"
    with raises(ProgrammingError):
        db.run_sql(sql1, params=dict(name="Test"), raise_errors=True)


def test_sql_object(db):
    sql = SQL("SELECT name FROM {table} WHERE name = {name}")
    params = dict(table=Identifier("sample"), name=Literal("Test"))

    res = list(db.run_sql(sql, raise_errors=True, params=params))
    assert len(res) == 1
    assert res[0].scalar() == "Test"


def test_sqlalchemy_bound_parameters(db):
    """Some of the parameters should be pre-bound."""
    sql = "SELECT {column} FROM {table} WHERE {column} = :value"
    params = dict(column=Identifier("name"), table=Identifier("sample"), value="Test")
    db.run_sql(sql, params=params, raise_errors=True)


def test_server_bound_parameters(db):
    """If we have Postgres-style string bind parameters, make sure we don't try to bind SQLAlchemy parameters."""
    sql = "SELECT name FROM sample WHERE name = %(name)s"
    params = dict(name="Test")
    res = list(db.run_sql(sql, params=params, raise_errors=True))
    assert len(res) == 1
    assert res[0].scalar() == "Test"


def test_server_parameters_function_def(db):
    """Make sure we don't select all % as bound parameters."""
    sql = """
    CREATE OR REPLACE FUNCTION throw_error()
    RETURNS void AS $$
    BEGIN
    IF true THEN
        RAISE NOTICE 'prop %s, pattern %, schema %', prop, pattern, schema->'patternProperties'->pattern;
    END IF;
    END;
    $$ LANGUAGE plpgsql;
    """
    with raises(TypeError):
        db.run_sql(sql, raise_errors=True)
    # This should not raise
    db.run_sql(sql, raise_errors=True, has_server_binds=False)


def test_long_running_sql(db):
    sql = "SELECT pg_sleep(0.5)"
    res = list(db.run_sql(sql, raise_errors=True))
    assert len(res) == 1
    assert res[0].scalar() == ""


def test_close_connection(conn):
    """
    Basic test demonstrating the underlying capability to kill a long-running query
    by closing the connection to the database.
    """

    import threading
    from psycopg2.extensions import QueryCanceledError
    from sqlalchemy.exc import DBAPIError

    sql = text("SELECT pg_sleep(10)")

    seconds = 1
    t = threading.Timer(seconds, conn.connection.cancel)
    t.start()

    try:
        conn.execute(sql)
        assert False
    except DBAPIError as e:
        if type(e.orig) == QueryCanceledError:
            print("Long running query was cancelled.")
            assert True
    t.cancel()


def test_sigint_cancel(db):
    """
    Basic test demonstrating the underlying capability to kill a long-running query
    by sending a SIGINT.
    """

    import subprocess
    import signal
    import time

    db_url = str(db.engine.url)

    # Time how long it takes to run the script
    start = time.time()

    script = relative_path(__file__, "test-scripts/test-long-running-query")

    p = subprocess.Popen(
        [
            str(script),
            db_url,
        ],
    )
    time.sleep(0.5)
    p.send_signal(signal.SIGINT)
    p.wait()
    assert p.returncode == 1
    # Make sure it didn't take too long
    dT = time.time() - start
    assert dT < 2


def test_check_table_exists(db):
    tables = db.inspector.get_table_names()
    assert "sample" in tables
    assert "samplea" not in tables


def test_check_table_exists_postgresql(db):
    assert table_exists(db, "sample")
    assert not table_exists(db, "samplea")
