"""Flask blueprint for the optional History dashboard tab.

All endpoints are read-only JSON behind the analytics server's existing
authentication (``DELETE /history/streamer/<name>`` is the one write).  They
return ``503`` until History has initialized its database, which also keeps
the endpoints safe in tests or when History failed to start.
"""

import logging
import sqlite3

from flask import Blueprint, jsonify, request

from TwitchChannelPointsMiner.history import (
    HistoryUnavailableError,
    delete_streamer_history,
    get_store,
    import_status,
    is_recording,
)

logger = logging.getLogger(__name__)

bp = Blueprint("history", __name__, url_prefix="/history")

MAX_DAYS = 3650


class HistoryApiError(Exception):
    def __init__(self, message, status=400):
        super(HistoryApiError, self).__init__(message)
        self.message = message
        self.status = status


@bp.errorhandler(HistoryApiError)
def _handle_api_error(error):
    return jsonify({"error": error.message}), error.status


@bp.errorhandler(HistoryUnavailableError)
@bp.errorhandler(sqlite3.Error)
@bp.errorhandler(OSError)
def _handle_database_error(error):
    logger.warning("History database is temporarily unavailable: %s", error)
    return (
        jsonify({"error": "History is temporarily unavailable. Try again shortly."}),
        503,
    )


def _int_arg(name, default, minimum, maximum):
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise HistoryApiError(f"'{name}' must be an integer")
    if value < minimum or value > maximum:
        raise HistoryApiError(f"'{name}' must be between {minimum} and {maximum}")
    return value


def _streamer_arg():
    value = (request.args.get("streamer") or "").strip().lower()
    if len(value) > 100:
        raise HistoryApiError("'streamer' is too long")
    return value or None


def _store():
    store = get_store()
    if store is None or not store.exists():
        raise HistoryApiError(
            "History is not available yet. It may still be starting.",
            status=503,
        )
    return store


@bp.route("/overview", methods=["GET"])
def overview():
    days = _int_arg("days", 30, 1, MAX_DAYS)
    return jsonify(_store().overview(days=days))


@bp.route("/streamers", methods=["GET"])
def streamers():
    days = _int_arg("days", 30, 1, MAX_DAYS)
    return jsonify({"days": days, "streamers": _store().streamers(days=days)})


@bp.route("/streamer/<string:streamer>", methods=["GET"])
def streamer_detail(streamer):
    days = _int_arg("days", 90, 1, MAX_DAYS)
    name = str(streamer or "").strip().lower()
    if not name or len(name) > 100:
        raise HistoryApiError("Invalid streamer name")
    return jsonify(_store().streamer_detail(name, days=days))


@bp.route("/streamer/<string:streamer>", methods=["DELETE"])
def delete_streamer(streamer):
    name = str(streamer or "").strip().lower()
    if not name or len(name) > 100:
        raise HistoryApiError("Invalid streamer name")
    deleted = delete_streamer_history(name, store=_store())
    return jsonify({"streamer": name, "deleted": deleted})


@bp.route("/daily", methods=["GET"])
def daily():
    days = _int_arg("days", 30, 1, MAX_DAYS)
    return jsonify(
        {
            "days": days,
            "daily": _store().daily(days=days, streamer=_streamer_arg()),
        }
    )


@bp.route("/sessions", methods=["GET"])
def sessions():
    kind = (request.args.get("kind") or "watch").strip().lower()
    if kind not in {"watch", "stream", "run"}:
        raise HistoryApiError("'kind' must be watch, stream, or run")
    return jsonify(
        _store().sessions(
            kind=kind,
            days=_int_arg("days", 30, 1, MAX_DAYS),
            streamer=_streamer_arg(),
            limit=_int_arg("limit", 100, 1, 500),
            offset=_int_arg("offset", 0, 0, 1000000),
        )
    )


@bp.route("/events", methods=["GET"])
def events():
    return jsonify(
        _store().events(
            event_type=(request.args.get("type") or "").strip() or None,
            days=_int_arg("days", 30, 1, MAX_DAYS),
            streamer=_streamer_arg(),
            limit=_int_arg("limit", 100, 1, 500),
            offset=_int_arg("offset", 0, 0, 1000000),
        )
    )


@bp.route("/games", methods=["GET"])
def games():
    return jsonify(
        _store().games(
            days=_int_arg("days", 30, 1, MAX_DAYS),
            streamer=_streamer_arg(),
        )
    )


@bp.route("/drops", methods=["GET"])
def drops():
    game = (request.args.get("game") or "").strip()
    if len(game) > 200:
        raise HistoryApiError("'game' is too long")
    return jsonify(
        _store().drops(
            days=_int_arg("days", 30, 1, MAX_DAYS),
            streamer=_streamer_arg(),
            game=game or None,
            limit=_int_arg("limit", 100, 1, 500),
            offset=_int_arg("offset", 0, 0, 1000000),
        )
    )


@bp.route("/activity", methods=["GET"])
def activity():
    return jsonify(
        _store().activity(
            days=_int_arg("days", 30, 1, 366),
            streamer=_streamer_arg(),
        )
    )


@bp.route("/health", methods=["GET"])
def health():
    store = _store()
    return jsonify(
        {
            "ready": True,
            "recording": is_recording(),
            "database": store.path,
            "journal": store.journal_status(),
            "analytics_import": import_status(),
        }
    )


def register_history(app):
    """Register the blueprint once on an analytics Flask app."""

    if "history" in app.blueprints:
        return
    app.register_blueprint(bp)
