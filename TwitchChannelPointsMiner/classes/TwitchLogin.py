# Based on https://github.com/derrod/twl.py
# Original Copyright (c) 2020 Rodney
# The MIT License (MIT)

import copy

# import getpass
import io
import json
import logging
import os
import pickle
from datetime import datetime, timedelta, timezone
from time import sleep

import requests

from TwitchChannelPointsMiner.classes.Exceptions import WrongCookiesException
from TwitchChannelPointsMiner.constants import CLIENT_ID, USER_AGENTS, GQLOperations

logger = logging.getLogger(__name__)


class _LegacyCookieUnpickler(pickle.Unpickler):
    """Read old data-only cookie pickles without allowing global imports."""

    def find_class(self, module, name):
        raise pickle.UnpicklingError("global objects are not allowed in cookie files")


def _validate_cookies(cookies):
    if not isinstance(cookies, list):
        raise ValueError("cookie data must be a list")
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise ValueError("each cookie must be an object")
        if not isinstance(cookie.get("name"), str):
            raise ValueError("each cookie must have a string name")
        if cookie.get("value") is not None and not isinstance(cookie["value"], str):
            raise ValueError("cookie values must be strings or null")
    return cookies


class TwitchLogin(object):
    __slots__ = [
        "client_id",
        "device_id",
        "token",
        "login_check_result",
        "session",
        "session",
        "username",
        "user_id",
        "email",
        "cookies",
    ]

    def __init__(self, client_id, device_id, username, user_agent):
        self.client_id = client_id
        self.device_id = device_id
        self.token = None
        self.login_check_result = False
        self.session = requests.session()
        self.session.headers.update(
            {
                "Client-ID": self.client_id,
                "X-Device-Id": self.device_id,
                "User-Agent": user_agent,
            }
        )
        self.username = username
        self.user_id = None
        self.email = None

        self.cookies = []

    def login_flow(self):
        logger.info("You'll have to login to Twitch!")

        post_data = {
            "client_id": self.client_id,
            "scopes": (
                "channel_read chat:read user_blocks_edit "
                "user_blocks_read user_follows_edit user_read"
            ),
        }
        while True:
            logger.info("Trying the TV login method..")

            login_response = self.send_oauth_request(
                "https://id.twitch.tv/oauth2/device", post_data
            )

            # {
            #     "device_code": "40 chars [A-Za-z0-9]",
            #     "expires_in": 1800,
            #     "interval": 5,
            #     "user_code": "8 chars [A-Z]",
            #     "verification_uri": "https://www.twitch.tv/activate"
            # }

            if login_response.status_code != 200:
                logger.error("TV login response is not 200. Try again")
                break

            login_response_json = login_response.json()

            if "user_code" in login_response_json:
                user_code: str = login_response_json["user_code"]
                now = datetime.now(timezone.utc)
                device_code: str = login_response_json["device_code"]
                interval: int = login_response_json["interval"]
                expires_at = now + timedelta(seconds=login_response_json["expires_in"])
                logger.info("Open https://www.twitch.tv/activate")
                logger.info(f"and enter this code: {user_code}")
                logger.info(
                    f"Hurry up! It will expire in {int(login_response_json['expires_in'] / 60)} minutes!"
                )
                # twofa = input("2FA token: ")
                # webbrowser.open_new_tab("https://www.twitch.tv/activate")

                post_data = {
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }

                while True:
                    # sleep first, not like the user is gonna enter the code *that* fast
                    sleep(interval)
                    login_response = self.send_oauth_request(
                        "https://id.twitch.tv/oauth2/token", post_data
                    )
                    if now == expires_at:
                        logger.error("Code expired. Try again")
                        break
                    # 200 means success, 400 means the user haven't entered the code yet
                    if login_response.status_code != 200:
                        continue
                    # {
                    #     "access_token": "40 chars [A-Za-z0-9]",
                    #     "refresh_token": "40 chars [A-Za-z0-9]",
                    #     "scope": [...],
                    #     "token_type": "bearer"
                    # }
                    login_response_json = login_response.json()
                    if "access_token" in login_response_json:
                        self.set_token(login_response_json["access_token"])
                        return self.check_login()
                    # except RequestInvalid:
                    # the device_code has expired, request a new code
                    # continue
                    # invalidate_after is not None
                    # account for the expiration landing during the request
                    # and datetime.now(timezone.utc) >= (invalidate_after - session_timeout)
                    # ):
                    # raise RequestInvalid()
                    else:
                        if "error_code" in login_response:
                            err_code = login_response["error_code"]

                        logger.error(f"Unknown error: {login_response}")
                        raise NotImplementedError(
                            f"Unknown TwitchAPI error code: {err_code}"
                        )

        return False

    def set_token(self, new_token):
        self.token = new_token
        self.session.headers.update({"Authorization": f"Bearer {self.token}"})

    # def send_login_request(self, json_data):
    def send_oauth_request(self, url, json_data):
        # response = self.session.post("https://passport.twitch.tv/protected_login", json=json_data)
        """response = self.session.post("https://passport.twitch.tv/login", json=json_data, headers={
            'Accept': 'application/vnd.twitchtv.v3+json',
            'Accept-Encoding': 'gzip',
            'Accept-Language': 'en-US',
            'Content-Type': 'application/json; charset=UTF-8',
            'Host': 'passport.twitch.tv'
        },)"""
        response = self.session.post(
            url,
            data=json_data,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Accept-Language": "en-US",
                "Cache-Control": "no-cache",
                "Client-Id": CLIENT_ID,
                "Host": "id.twitch.tv",
                "Origin": "https://android.tv.twitch.tv",
                "Pragma": "no-cache",
                "Referer": "https://android.tv.twitch.tv/",
                "User-Agent": USER_AGENTS["Android"]["TV"],
                "X-Device-Id": self.device_id,
            },
            timeout=(5, 30),
        )
        return response

    def check_login(self):
        if self.login_check_result:
            return self.login_check_result
        if self.token is None:
            return False

        self.login_check_result = self.__set_user_id()
        return self.login_check_result

    def save_cookies(self, cookies_file):
        logger.info("Saving cookies to your computer..")
        cookies_dict = self.session.cookies.get_dict()
        # print(f"cookies_dict2pickle: {cookies_dict}")
        cookies_dict["auth-token"] = self.token
        if "persistent" not in cookies_dict:  # saving user id cookies
            cookies_dict["persistent"] = self.user_id

        # old way saves only 'auth-token' and 'persistent'
        self.cookies = []
        # print(f"cookies_dict2pickle: {cookies_dict}")
        for cookie_name, value in cookies_dict.items():
            self.cookies.append({"name": cookie_name, "value": value})
        # print(f"cookies2pickle: {self.cookies}")
        self._write_cookies(cookies_file)

    def _write_cookies(self, cookies_file):
        temporary = f"{cookies_file}.tmp"
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as cookie_handle:
                json.dump(self.cookies, cookie_handle)
            os.chmod(temporary, 0o600)
            os.replace(temporary, cookies_file)
            os.chmod(cookies_file, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get_cookie_value(self, key):
        for cookie in self.cookies:
            if cookie["name"] == key:
                if cookie["value"] is not None:
                    return cookie["value"]
        return None

    def load_cookies(self, cookies_file):
        if not os.path.isfile(cookies_file):
            raise WrongCookiesException("There must be a cookies file!")

        with open(cookies_file, "rb") as cookie_handle:
            serialized = cookie_handle.read()

        try:
            cookies = json.loads(serialized.decode("utf-8"))
            legacy_format = False
        except (UnicodeDecodeError, json.JSONDecodeError):
            try:
                cookies = _LegacyCookieUnpickler(io.BytesIO(serialized)).load()
                legacy_format = True
            except (pickle.UnpicklingError, EOFError, ValueError, TypeError) as error:
                raise WrongCookiesException(
                    "The cookies file is invalid or unsafe"
                ) from error

        try:
            self.cookies = _validate_cookies(cookies)
        except ValueError as error:
            raise WrongCookiesException(
                "The cookies file has an invalid structure"
            ) from error

        os.chmod(cookies_file, 0o600)
        if legacy_format:
            logger.info("Migrating the legacy cookie file to JSON...")
            self._write_cookies(cookies_file)

    def get_user_id(self):
        persistent = self.get_cookie_value("persistent")
        user_id = (
            int(persistent.split("%")[0]) if persistent is not None else self.user_id
        )
        if user_id is None:
            if self.__set_user_id() is True:
                return self.user_id
        return user_id

    def __set_user_id(self):
        json_data = copy.deepcopy(GQLOperations.GetIDFromLogin)
        json_data["variables"]["login"] = self.username
        response = self.session.post(GQLOperations.url, json=json_data, timeout=(5, 30))

        if response.status_code == 200:
            json_response = response.json()
            if (
                "data" in json_response
                and "user" in json_response["data"]
                and json_response["data"]["user"]["id"] is not None
            ):
                self.user_id = json_response["data"]["user"]["id"]
                return True
        return False

    def get_auth_token(self):
        return self.get_cookie_value("auth-token")
