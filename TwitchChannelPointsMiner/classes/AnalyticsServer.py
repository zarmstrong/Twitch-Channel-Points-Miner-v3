import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from threading import Thread

import pandas as pd
from flask import Flask, Response, cli, render_template, request
from werkzeug.serving import WSGIRequestHandler

from TwitchChannelPointsMiner.classes.Settings import Settings

cli.show_server_banner = lambda *_: None
logger = logging.getLogger(__name__)


def get_assets_folder():
    repository_assets = Path(__file__).resolve().parents[2] / "assets"
    if repository_assets.is_dir():
        return str(repository_assets)

    return str(Path().absolute() / "assets")


def streamers_available():
    path = Settings.analytics_path
    excluded_files = {"drops_by_category.json"}
    return [
        f
        for f in os.listdir(path)
        if os.path.isfile(os.path.join(path, f))
        and f.endswith(".json")
        and f not in excluded_files
    ]


def aggregate(df, freq="30Min"):
    df_base_events = df[(df.z == "Watch") | (df.z == "Claim")]
    df_other_events = df[(df.z != "Watch") & (df.z != "Claim")]

    be = df_base_events.groupby([pd.Grouper(freq=freq, key="datetime"), "z"]).max()
    be = be.reset_index()

    oe = df_other_events.groupby([pd.Grouper(freq=freq, key="datetime"), "z"]).max()
    oe = oe.reset_index()

    result = pd.concat([be, oe])
    return result


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

    original_series = datas.get("series", [])

    if "series" in datas:
        df = pd.DataFrame(datas["series"])
        df["datetime"] = pd.to_datetime(df.x // 1000, unit="s")

        df = df[(df.x >= start_date) & (df.x <= end_date)]

        datas["series"] = (
            df.drop(columns="datetime")
            .sort_values(by=["x", "y"], ascending=True)
            .to_dict("records")
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

        new_end_date = start_date
        new_start_date = 0
        df = pd.DataFrame(original_series)
        df["datetime"] = pd.to_datetime(df.x // 1000, unit="s")

        # Attempt to get the last known balance from before the provided timeframe
        df = df[(df.x >= new_start_date) & (df.x <= new_end_date)]
        if df.empty:
            datas["series"] = []
            datas["annotations"] = []
            return datas
        last_balance = (
            df.drop(columns="datetime")
            .sort_values(by=["x", "y"], ascending=True)
            .to_dict("records")[-1]["y"]
        )

        datas["series"] = [
            {"x": start_date, "y": last_balance, "z": "No Stream"},
            {"x": end_date, "y": last_balance, "z": "No Stream"},
        ]

    if "annotations" in datas:
        df = pd.DataFrame(datas["annotations"])
        df["datetime"] = pd.to_datetime(df.x // 1000, unit="s")

        df = df[(df.x >= start_date) & (df.x <= end_date)]

        datas["annotations"] = (
            df.drop(columns="datetime")
            .sort_values(by="x", ascending=True)
            .to_dict("records")
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


def get_challenge_points(streamer):
    datas = read_json(streamer, return_response=False)
    if "series" in datas and datas["series"]:
        return datas["series"][-1]["y"]
    return 0  # Default value when 'series' key is not found or empty


def get_last_activity(streamer):
    datas = read_json(streamer, return_response=False)
    if "series" in datas and datas["series"]:
        return datas["series"][-1]["x"]
    return 0  # Default value when 'series' key is not found or empty


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
        return Response(
            json.dumps({"categories": {}, "drops": []}),
            status=200,
            mimetype="application/json",
        )

    try:
        with open(drops_file, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (json.JSONDecodeError, OSError):
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

    return Response(
        json.dumps({"categories": grouped, "drops": drops}),
        status=200,
        mimetype="application/json",
    )


def index(refresh=5, days_ago=7):
    return render_template(
        "charts.html",
        refresh=(refresh * 60 * 1000),
        daysAgo=days_ago,
    )


def streamers():
    return Response(
        json.dumps(
            [
                {
                    "name": s,
                    "points": get_challenge_points(s),
                    "last_activity": get_last_activity(s),
                }
                for s in sorted(streamers_available())
            ]
        ),
        status=200,
        mimetype="application/json",
    )


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
    ):
        super(AnalyticsServer, self).__init__()

        check_assets()

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
            try:
                last_received_index = int(raw_position)
            except (TypeError, ValueError):
                return Response("Invalid log position.", status=400)
            if last_received_index < 0:
                return Response("Invalid log position.", status=400)

            logs_path = os.path.join(Path().absolute(), "logs")
            log_file_path = os.path.join(logs_path, f"{username}.log")
            try:
                file_size = os.path.getsize(log_file_path)
                position = (
                    last_received_index if last_received_index <= file_size else 0
                )
                with open(log_file_path, "rb") as log_file:
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
            defaults={"refresh": refresh, "days_ago": days_ago},
            methods=["GET"],
        )
        self.app.add_url_rule("/streamers", "streamers", streamers, methods=["GET"])
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
