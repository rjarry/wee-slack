from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum
from io import StringIO
import json
import os
import resource
from typing import (
    Any,
    Dict,
    Generator,
    Generic,
    NamedTuple,
    TYPE_CHECKING,
    Tuple,
    TypeVar,
    Union,
)
from typing import Awaitable, Coroutine
from urllib.parse import urlencode
from uuid import uuid4

import weechat


if TYPE_CHECKING:
    from slack_api import (
        SlackConversation,
        SlackConversationNotIm,
        SlackConversationIm,
    )
else:
    # To support running without slack types
    SlackConversation = Any
    SlackConversationNotIm = Any
    SlackConversationIm = Any

SCRIPT_NAME = "slack"
SCRIPT_AUTHOR = "Trygve Aaberge <trygveaa@gmail.com>"
SCRIPT_VERSION = "3.0.0"
SCRIPT_LICENSE = "MIT"
SCRIPT_DESC = "Extends weechat for typing notification/search/etc on slack.com"
REPO_URL = "https://github.com/wee-slack/wee-slack"

### Basic classes

T = TypeVar("T")


class LogLevel(IntEnum):
    TRACE = 1
    DEBUG = 2
    INFO = 3
    WARN = 4
    ERROR = 5
    FATAL = 6


class HttpError(Exception):
    def __init__(self, url: str, return_code: int, http_status: int, error: str):
        super().__init__()
        self.url = url
        self.return_code = return_code
        self.http_status = http_status
        self.error = error


class Future(Awaitable[T]):
    def __init__(self):
        self.id = str(uuid4())

    def __await__(self) -> Generator[Future[T], T, T]:
        return (yield self)


class FutureProcess(Future[Tuple[str, int, str, str]]):
    pass


class FutureTimer(Future[Tuple[int]]):
    pass


class Task(Future[T]):
    def __init__(self, coroutine: Coroutine[Future[Any], Any, T], final: bool):
        super().__init__()
        self.coroutine = coroutine
        self.final = final


### Helpers


def log(level: LogLevel, message: str):
    if level >= LogLevel.INFO:
        print(level, message)


def available_file_descriptors():
    num_current_file_descriptors = len(os.listdir("/proc/self/fd/"))
    max_file_descriptors = min(resource.getrlimit(resource.RLIMIT_NOFILE))
    return max_file_descriptors - num_current_file_descriptors


### WeeChat classes


class WeeChatColor(str):
    pass


@dataclass
class WeeChatConfig:
    name: str

    def __post_init__(self):
        self.pointer = weechat.config_new(self.name, "", "")


@dataclass
class WeeChatSection:
    config: WeeChatConfig
    name: str
    user_can_add_options: bool = False
    user_can_delete_options: bool = False

    def __post_init__(self):
        self.pointer = weechat.config_new_section(
            self.config.pointer,
            self.name,
            self.user_can_add_options,
            self.user_can_delete_options,
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        )


WeeChatOptionType = TypeVar("WeeChatOptionType", bool, int, WeeChatColor, str)


@dataclass
class WeeChatOption(Generic[WeeChatOptionType]):
    default_section: WeeChatSection
    workspace_section: WeeChatSection
    name: str
    description: str
    default_value: WeeChatOptionType
    string_values: str
    min_value: int
    max_value: int

    def __post_init__(self):
        self.default_pointer = self._config_new_option()
        self.workspace_pointers: Dict[str, str] = {}

    @property
    def weechat_type(self) -> str:
        if isinstance(self.default_value, bool):
            return "boolean"
        if isinstance(self.default_value, int):
            return "integer"
        if isinstance(self.default_value, WeeChatColor):
            return "color"
        return "string"

    @property
    def default_value_str(self) -> str:
        return str(self.default_value)

    def get_value(self, workspace_name: str) -> WeeChatOptionType:
        option_pointer = (
            self.workspace_pointers.get(workspace_name) or self.default_pointer
        )
        if weechat.config_option_is_null(option_pointer):
            option_pointer = self.default_pointer

        if isinstance(self.default_value, bool):
            return weechat.config_boolean(option_pointer) == 1
        if isinstance(self.default_value, int):
            return weechat.config_integer(option_pointer)
        if isinstance(self.default_value, WeeChatColor):
            color = weechat.config_color(option_pointer)
            return WeeChatColor(color)
        return weechat.config_string(option_pointer)

    def add_workspace(self, workspace_name: str):
        workspace_pointer = self._config_new_option(workspace_name)
        self.workspace_pointers[workspace_name] = workspace_pointer

    def _config_new_option(
        self,
        workspace_name: str | None = None,
    ) -> str:
        if workspace_name:
            name = f"{workspace_name}.{self.name} << {self.default_section.config.name}.{self.default_section.name}.{self.name}"
            section = self.workspace_section
            default_value = None
            null_value_allowed = True
        else:
            name = self.name
            section = self.default_section
            default_value = self.default_value_str
            null_value_allowed = False

        value = None

        if weechat_version < 0x3050000:
            default_value = self.default_value_str
            value = default_value

        return weechat.config_new_option(
            section.config.pointer,
            section.pointer,
            name,
            self.weechat_type,
            self.description,
            self.string_values,
            self.min_value,
            self.max_value,
            default_value,
            value,
            null_value_allowed,
            "",
            "",
            "",
            "",
            "",
            "",
        )


### WeeChat callbacks

active_tasks: Dict[str, Task[Any]] = {}
active_responses: Dict[str, Tuple[Any, ...]] = {}


def shutdown_cb():
    weechat.config_write(config.config.pointer)
    return weechat.WEECHAT_RC_OK


def weechat_task_cb(data: str, *args: Any) -> int:
    task = active_tasks.pop(data)
    task_runner(task, args)
    return weechat.WEECHAT_RC_OK


### WeeChat helpers


def task_runner(task: Task[Any], response: Any):
    while True:
        try:
            future = task.coroutine.send(response)
            if future.id in active_responses:
                response = active_responses.pop(future.id)
            else:
                if future.id in active_tasks:
                    raise Exception(
                        f"future.id in active_tasks, {future.id}, {active_tasks}"
                    )
                active_tasks[future.id] = task
                break
        except StopIteration as e:
            if task.id in active_tasks:
                task = active_tasks.pop(task.id)
                response = e.value
            else:
                if task.id in active_responses:
                    raise Exception(
                        f"task.id in active_responses, {task.id}, {active_responses}"
                    )
                if not task.final:
                    active_responses[task.id] = e.value
                break


def create_task(
    coroutine: Coroutine[Future[Any], Any, T], final: bool = False
) -> Task[T]:
    task = Task(coroutine, final)
    task_runner(task, None)
    return task


async def sleep(milliseconds: int):
    future = FutureTimer()
    weechat.hook_timer(milliseconds, 0, 1, weechat_task_cb.__name__, future.id)
    return await future


async def hook_process_hashtable(command: str, options: Dict[str, str], timeout: int):
    future = FutureProcess()
    log(
        LogLevel.DEBUG,
        f"hook_process_hashtable calling ({future.id}): command: {command}",
    )
    while available_file_descriptors() < 10:
        await sleep(10)
    weechat.hook_process_hashtable(
        command, options, timeout, weechat_task_cb.__name__, future.id
    )

    stdout = StringIO()
    stderr = StringIO()
    return_code = -1

    while return_code == -1:
        _, return_code, out, err = await future
        log(
            LogLevel.TRACE,
            f"hook_process_hashtable intermediary response ({future.id}): command: {command}",
        )
        stdout.write(out)
        stderr.write(err)

    out = stdout.getvalue()
    err = stderr.getvalue()
    log(
        LogLevel.DEBUG,
        f"hook_process_hashtable response ({future.id}): command: {command}, "
        f"return_code: {return_code}, response length: {len(out)}"
        + (f", error: {err}" if err else ""),
    )

    return command, return_code, out, err


async def http_request(
    url: str, options: Dict[str, str], timeout: int, max_retries: int = 5
) -> str:
    options["header"] = "1"
    _, return_code, out, err = await hook_process_hashtable(
        f"url:{url}", options, timeout
    )

    if return_code != 0 or err:
        if max_retries > 0:
            log(
                LogLevel.INFO,
                f"HTTP error, retrying (max {max_retries} times): "
                f"return_code: {return_code}, error: {err}, url: {url}",
            )
            await sleep(1000)
            return await http_request(url, options, timeout, max_retries - 1)
        raise HttpError(url, return_code, 0, err)

    headers_end_index = out.index("\r\n\r\n")
    headers = out[:headers_end_index].split("\r\n")
    http_status = int(headers[0].split(" ")[1])

    if http_status == 429:
        for header in headers[1:]:
            name, value = header.split(":", 1)
            if name.lower() == "retry-after":
                retry_after = int(value.strip())
                log(
                    LogLevel.INFO,
                    f"HTTP ratelimit, retrying in {retry_after} seconds, url: {url}",
                )
                await sleep(retry_after * 1000)
                return await http_request(url, options, timeout)

    body = out[headers_end_index + 4 :]

    if http_status >= 400:
        raise HttpError(url, return_code, http_status, body)

    return body


### Slack Classes


class SlackConfig:
    def __init__(self):
        self.config = WeeChatConfig("slack")
        self.section_workspace_default = WeeChatSection(
            self.config, "workspace_default"
        )
        self.section_workspace = WeeChatSection(self.config, "workspace")

        self.slack_timeout = WeeChatOption(
            self.section_workspace_default,
            self.section_workspace,
            "slack_timeout",
            "timeout (in seconds) for network requests",
            30,
            "",
            0,
            3600,
        )

        self.slack_timeout.add_workspace("wee-slack-test")

        # for attr in vars(self).values():
        #     if isinstance(attr, WeeChatOption):
        #         print(attr.weechat_type)

        weechat.config_read(self.config.pointer)
        weechat.config_write(self.config.pointer)


class SlackToken(NamedTuple):
    token: str
    cookie: Union[str, None] = None


class SlackApi:
    def __init__(self, team: SlackTeam, token: SlackToken):
        self.team = team
        self.token = token

    def get_request_options(self):
        cookies = f"d={self.token.cookie}" if self.token.cookie else ""
        return {
            "useragent": f"wee_slack {SCRIPT_VERSION}",
            "httpheader": f"Authorization: Bearer {self.token.token}",
            "cookie": cookies,
        }

    async def fetch(self, method: str, params: Dict[str, Union[str, int]] = {}):
        url = f"https://api.slack.com/api/{method}?{urlencode(params)}"
        response = await http_request(
            url,
            self.get_request_options(),
            config.slack_timeout.get_value(self.team.name),
        )
        return json.loads(response)

    async def fetch_list(
        self,
        method: str,
        list_key: str,
        params: Dict[str, Union[str, int]] = {},
        pages: int = 1,  # negative or 0 means all pages
    ):
        response = await self.fetch(method, params)
        next_cursor = response.get("response_metadata", {}).get("next_cursor")
        if pages != 1 and next_cursor and response["ok"]:
            params["cursor"] = next_cursor
            next_pages = await self.fetch_list(method, list_key, params, pages - 1)
            response[list_key].extend(next_pages[list_key])
            return response
        return response


class SlackTeam:
    def __init__(self, token: SlackToken, name: str):
        self.api = SlackApi(self, token)
        self.name = name


class SlackChannelCommonNew:
    def __init__(self, team: SlackTeam, slack_info: SlackConversation):
        self.team = team
        self.api = team.api
        self.id = slack_info["id"]
        # self.fetch_info()

    async def fetch_info(self):
        response = await self.api.fetch("conversations.info", {"channel": self.id})
        print(len(response))


class SlackChannelNew(SlackChannelCommonNew):
    def __init__(self, team: SlackTeam, slack_info: SlackConversationNotIm):
        super().__init__(team, slack_info)
        self.name = slack_info["name"]


class SlackIm(SlackChannelCommonNew):
    def __init__(self, team: SlackTeam, slack_info: SlackConversationIm):
        super().__init__(team, slack_info)
        self.user = slack_info["user"]


async def init():
    token = SlackToken(
        weechat.config_get_plugin("api_token"), weechat.config_get_plugin("api_cookie")
    )
    team = SlackTeam(token, "wee-slack-test")
    print(team)
    print(config.slack_timeout.get_value(team.name))
    # print(team.config.slack_timeout)


if __name__ == "__main__":
    if weechat.register(
        SCRIPT_NAME,
        SCRIPT_AUTHOR,
        SCRIPT_VERSION,
        SCRIPT_LICENSE,
        SCRIPT_DESC,
        "shutdown_cb",
        "",
    ):
        weechat_version = int(weechat.info_get("version_number", "") or 0)
        config = SlackConfig()
        create_task(init(), final=True)