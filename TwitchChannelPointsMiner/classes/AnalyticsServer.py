import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from threading import Thread

from flask import Flask, Response, cli, render_template, request
from werkzeug.serving import WSGIRequestHandler

from TwitchChannelPointsMiner.classes.Settings import ANALYTICS_FILE_MUTEX, Settings

cli.show_server_banner = lambda *_: None
logger = logging.getLogger(__name__)

MAX_LOG_TAIL_BYTES = 1024 * 1024


def bounded_log_start(file_size, last_received_index, tail_bytes=None):
    """Return a safe absolute offset for an analytics log response.

    Older cached dashboard clients may omit ``tailBytes`` and repeatedly ask for
    the log from byte zero.  Never let one request read an unbounded log into
    memory and monopolize the miner process.
    """
    position = last_received_index if last_received_index <= file_size else 0
    response_limit = min(tail_bytes or MAX_LOG_TAIL_BYTES, MAX_LOG_TAIL_BYTES)
    return max(position, file_size - response_limit)


def get_assets_folder():
    repository_assets = Path(__file__).resolve().parents[2] / "assets"
    if repository_assets.is_dir():
        return str(repository_assets)

    return str(Path().absolute() / "assets")


def streamers_available():
    path = Settings.analytics_path
    excluded_files = {"drops_by_category.json"}
    available = [
        f
        for f in os.listdir(path)
        if os.path.isfile(os.path.join(path, f))
        and f.endswith(".json")
        and f not in excluded_files
    ]
    logger.debug("Analytics points scan path='%s' files=%s", path, sorted(available))
    return available


def filter_datas(start_date, end_date, datas):
    # Note: https://stackoverflow.com/questions/4676195/why-do-i-need-to-multiply-unix-timestamps-by-1000-in-javascript
    start_date = (
        datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000
        if start_date is not None
        else 0
    )
    end_date = (
        datetime.strptime(end_date, "%Y-%m-%d")
        if end_date is not None
        else datetime.now()
    ).replace(hour=23, minute=59, second=59).timestamp() * 1000

    raw_series = datas.get("series", [])
    original_series = raw_series if isinstance(raw_series, list) else []
    if not isinstance(datas.get("annotations", []), list):
        datas["annotations"] = []

    if original_series:
        datas["series"] = sorted(
            (
                entry
                for entry in original_series
                if isinstance(entry, dict)
                and start_date <= entry.get("x", -1) <= end_date
            ),
            key=lambda entry: (entry.get("x", 0), entry.get("y", 0)),
        )
    else:
        datas["series"] = []

    # If no data is found within the timeframe, that usually means the streamer hasn't streamed within that timeframe
    # We create a series that shows up as a straight line on the dashboard, with 'No Stream' as labels
    if len(datas["series"]) == 0:
        if len(original_series) == 0:
            datas["series"] = []
            if "annotations" not in datas:
                datas["annotations"] = []
            return datas

        # Attempt to get the last known balance from before the provided timeframe
        earlier_entries = [
            entry
            for entry in original_series
            if isinstance(entry, dict) and 0 <= entry.get("x", -1) <= start_date
        ]
        if earlier_entries == []:
            datas["series"] = []
            datas["annotations"] = []
            return datas
        last_balance = max(
            earlier_entries,
            key=lambda entry: (entry.get("x", 0), entry.get("y", 0)),
        ).get("y", 0)

        datas["series"] = [
            {"x": start_date, "y": last_balance, "z": "No Stream"},
            {"x": end_date, "y": last_balance, "z": "No Stream"},
        ]

    raw_annotations = datas.get("annotations", [])
    if isinstance(raw_annotations, list) and raw_annotations:
        datas["annotations"] = sorted(
            (
                annotation
                for annotation in raw_annotations
                if isinstance(annotation, dict)
                and start_date <= annotation.get("x", -1) <= end_date
            ),
            key=lambda annotation: annotation.get("x", 0),
        )
    else:
        datas["annotations"] = []

    return datas


def read_json(streamer, return_response=True):
    start_date = request.args.get("startDate", type=str)
    end_date = request.args.get("endDate", type=str)

    path = Settings.analytics_path
    streamer = streamer if streamer.endswith(".json") else f"{streamer}.json"

    # Check if the file exists before attempting to read it
    if not os.path.exists(os.path.join(path, streamer)):
        error_message = f"File '{streamer}' not found."
        logger.error(error_message)
        if return_response:
            return Response(
                json.dumps({"error": error_message}),
                status=404,
                mimetype="application/json",
            )
        else:
            return {"error": error_message}

    try:
        with open(os.path.join(path, streamer), "r") as file:
            data = json.load(file)
    except json.JSONDecodeError as e:
        error_message = f"Error decoding JSON in file '{streamer}': {str(e)}"
        logger.error(error_message)
        if return_response:
            return Response(
                json.dumps({"error": error_message}),
                status=500,
                mimetype="application/json",
            )
        else:
            return {"error": error_message}

    # Handle filtering data, if applicable
    filtered_data = filter_datas(start_date, end_date, data)
    if return_response:
        return Response(
            json.dumps(filtered_data), status=200, mimetype="application/json"
        )
    else:
        return filtered_data


def get_streamer_summary(streamer):
    """Read the latest points record without running the chart-data pipeline."""
    filename = streamer if streamer.endswith(".json") else f"{streamer}.json"
    file_path = os.path.join(Settings.analytics_path, filename)
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            series = json.load(file).get("series", []) or []
    except (json.JSONDecodeError, OSError, AttributeError) as error:
        logger.error("Unable to read analytics summary '%s': %s", file_path, error)
        return {"points": 0, "last_activity": 0}

    if not isinstance(series, list):
        series = []

    latest = max(
        (entry for entry in series if isinstance(entry, dict)),
        key=lambda entry: entry.get("x", 0),
        default=None,
    )
    if latest is None:
        return {"points": 0, "last_activity": 0}
    return {
        "points": latest.get("y", 0),
        "last_activity": latest.get("x", 0),
    }


def json_all():
    return Response(
        json.dumps(
            [
                {
                    "name": streamer.strip(".json"),
                    "data": read_json(streamer, return_response=False),
                }
                for streamer in streamers_available()
            ]
        ),
        status=200,
        mimetype="application/json",
    )


def drops_by_category():
    drops_file = os.path.join(Settings.analytics_path, "drops_by_category.json")
    if os.path.isfile(drops_file) is False:
        logger.warning("Analytics Drops file not found: '%s'", drops_file)
        return Response(
            json.dumps({"categories": {}, "drops": []}),
            status=200,
            mimetype="application/json",
        )

    try:
        with open(drops_file, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError) as error:
        logger.error("Unable to read analytics Drops file '%s': %s", drops_file, error)
        return Response(
            json.dumps({"categories": {}, "drops": []}),
            status=200,
            mimetype="application/json",
        )

    drops = data.get("drops", [])
    grouped = {}
    for drop in drops:
        category = drop.get("category", "Unknown")
        if category not in grouped:
            grouped[category] = []
        grouped[category].append(drop)

    logger.debug(
        "Analytics Drops response path='%s' drops=%d categories=%d",
        drops_file,
        len(drops),
        len(grouped),
    )

    return Response(
        json.dumps({"categories": grouped, "drops": drops}),
        status=200,
        mimetype="application/json",
    )


def index(refresh=5, days_ago=7, log_poll_interval=5):
    assets_folder = get_assets_folder()
    asset_version = max(
        os.path.getmtime(os.path.join(assets_folder, filename))
        for filename in ("script.js", "style.css", "dark-theme.css")
    )
    return render_template(
        "charts.html",
        refresh=(refresh * 60 * 1000),
        daysAgo=days_ago,
        logPollInterval=(log_poll_interval * 1000),
        dateFormat=Settings.logger.date_format,
        assetVersion=int(asset_version),
    )


def streamers():
    available = sorted(streamers_available())
    response = []
    for streamer in available:
        summary = get_streamer_summary(streamer)
        response.append({"name": streamer, **summary})
    logger.debug(
        "Analytics points response path='%s' streamers=%d",
        Settings.analytics_path,
        len(response),
    )
    return Response(json.dumps(response), status=200, mimetype="application/json")


def delete_streamer_analytics(streamer):
    filename = streamer if streamer.endswith(".json") else f"{streamer}.json"

    # Only files returned by streamers_available() may be deleted. Besides
    # preventing path traversal, this protects non-streamer analytics files.
    if filename not in streamers_available():
        return Response(
            json.dumps({"error": f"Analytics data for '{streamer}' not found."}),
            status=404,
            mimetype="application/json",
        )

    with ANALYTICS_FILE_MUTEX:
        try:
            os.remove(os.path.join(Settings.analytics_path, filename))
        except FileNotFoundError:
            return Response(
                json.dumps({"error": f"Analytics data for '{streamer}' not found."}),
                status=404,
                mimetype="application/json",
            )
        except OSError as error:
            logger.error(f"Unable to delete analytics data in '{filename}': {error}")
            return Response(
                json.dumps({"error": "Unable to delete streamer analytics data."}),
                status=500,
                mimetype="application/json",
            )

    logger.info(f"Deleted analytics data in '{filename}'")
    return Response(status=204)


def check_assets():
    required_files = [
        "banner.png",
        "charts.html",
        "script.js",
        "style.css",
        "dark-theme.css",
    ]
    assets_folder = get_assets_folder()
    missing_files = [
        f for f in required_files if not os.path.isfile(os.path.join(assets_folder, f))
    ]
    if missing_files:
        raise FileNotFoundError(
            f"Missing analytics assets in {assets_folder}: {', '.join(missing_files)}"
        )


class AnalyticsWSGIRequestHandler(WSGIRequestHandler):
    def log_error(self, format, *args):
        message = format % args if args else format

        # Ignore TLS handshake bytes sent to the plain HTTP analytics endpoint.
        if "Bad request version" in message and "\\x" in message:
            return

        super().log_error(format, *args)


class AnalyticsServer(Thread):
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5000,
        refresh: int = 5,
        days_ago: int = 7,
        username: str = None,
        password: str = None,
        log_poll_interval: int = 5,
    ):
        super(AnalyticsServer, self).__init__()

        check_assets()

        if not isinstance(log_poll_interval, int) or isinstance(
            log_poll_interval, bool
        ):
            raise TypeError("Log polling interval must be an integer number of seconds")
        if not 1 <= log_poll_interval <= 180:
            raise ValueError("Log polling interval must be between 1 and 180 seconds")

        self.host = host
        self.port = port
        self.refresh = refresh
        self.days_ago = days_ago
        self.username = username
        self.password = password

        if host not in {"127.0.0.1", "localhost", "::1"} and not password:
            raise ValueError("Analytics exposed beyond localhost requires a password")

        def generate_log():
            raw_position = request.args.get("lastIndex", "0")
            raw_tail_bytes = request.args.get("tailBytes")
            try:
                last_received_index = int(raw_position)
            except (TypeError, ValueError):
                return Response("Invalid log position.", status=400)
            if last_received_index < 0:
                return Response("Invalid log position.", status=400)

            tail_bytes = None
            if raw_tail_bytes is not None:
                try:
                    tail_bytes = int(raw_tail_bytes)
                except (TypeError, ValueError):
                    return Response("Invalid log tail size.", status=400)
                if tail_bytes <= 0 or tail_bytes > MAX_LOG_TAIL_BYTES:
                    return Response("Invalid log tail size.", status=400)

            logs_path = os.path.join(Path().absolute(), "logs")
            log_file_path = os.path.join(logs_path, f"{username}.log")
            try:
                file_size = os.path.getsize(log_file_path)
                requested_position = (
                    last_received_index if last_received_index <= file_size else 0
                )
                position = bounded_log_start(
                    file_size, requested_position, tail_bytes=tail_bytes
                )
                with open(log_file_path, "rb") as log_file:
                    if position > requested_position:
                        log_file.seek(position)
                        # Avoid displaying the partial first line when the response
                        # was capped to a recent window.
                        log_file.readline()
                    else:
                        log_file.seek(position)
                    new_log_entries = log_file.read()
                    next_position = log_file.tell()

                return Response(
                    new_log_entries,
                    status=200,
                    mimetype="text/plain",
                    headers={"X-Log-Position": str(next_position)},
                )

            except (FileNotFoundError, OSError):
                return Response(
                    "Log file not found.", status=404, mimetype="text/plain"
                )

        self.app = Flask(
            __name__,
            template_folder=get_assets_folder(),
            static_folder=get_assets_folder(),
        )

        @self.app.before_request
        def require_authentication():
            if self.password is None:
                return None
            authorization = request.authorization
            valid_username = authorization is not None and secrets.compare_digest(
                authorization.username or "", self.username or ""
            )
            valid_password = authorization is not None and secrets.compare_digest(
                authorization.password or "", self.password
            )
            if valid_username and valid_password:
                return None
            return Response(
                "Authentication required.",
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="Twitch analytics"'},
            )

        self.app.add_url_rule(
            "/",
            "index",
            index,
            defaults={
                "refresh": refresh,
                "days_ago": days_ago,
                "log_poll_interval": log_poll_interval,
            },
            methods=["GET"],
        )
        self.app.add_url_rule("/streamers", "streamers", streamers, methods=["GET"])
        self.app.add_url_rule(
            "/streamers/<string:streamer>",
            "delete_streamer_analytics",
            delete_streamer_analytics,
            methods=["DELETE"],
        )
        self.app.add_url_rule(
            "/json/<string:streamer>", "json", read_json, methods=["GET"]
        )
        self.app.add_url_rule("/json_all", "json_all", json_all, methods=["GET"])
        self.app.add_url_rule(
            "/drops_by_category",
            "drops_by_category",
            drops_by_category,
            methods=["GET"],
        )
        self.app.add_url_rule("/log", "log", generate_log, methods=["GET"])

    def run(self):
        logger.info(
            f"Analytics running on http://{self.host}:{self.port}/",
            extra={"emoji": ":globe_with_meridians:"},
        )
        self.app.run(
            host=self.host,
            port=self.port,
            threaded=True,
            debug=False,
            request_handler=AnalyticsWSGIRequestHandler,
        )
