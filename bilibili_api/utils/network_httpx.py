"""
bilibili_api.utils.network_httpx

复写了 .utils.network，使用 httpx
"""

import asyncio
import atexit
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from functools import reduce
from inspect import iscoroutinefunction as isAsync
from typing import Any, Coroutine, Dict, Union

import httpx

from .. import settings
from ..exceptions import ResponseCodeException
from .Credential import Credential
from .utils import get_api
from .sync import sync

__session_pool = {}
last_proxy = ""
wbi_mixin_key = ""

# 使用 Referer 和 UA 请求头以绕过反爬虫机制
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com"}

@dataclass
class Api:
    url: str
    method: str
    comment: str = ""
    wbi: bool = False
    verify: bool = False
    no_csrf: bool = False
    json_body: bool = False
    ignore_code: bool = False
    data: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    credential: Credential = field(default_factory=Credential)

    def __post_init__(self):
        self.method = self.method.upper()
        self.original_data = self.data.copy()
        self.original_params = self.params.copy()
        self.data = {k: "" for k in self.data.keys()}
        self.params = {k: "" for k in self.params.keys()}
        if self.credential is None:
            self.credential = Credential()
        self.__result = None

    def __setattr__(self, __name: str, __value: Any) -> None:
        """
        每次更新参数都要把 __result 清除
        """
        if self.initialized and __name != "_Api__result":
            self.__result = None
        return super().__setattr__(__name, __value)

    @property
    def initialized(self):
        return "_Api__result" in self.__dict__

    @property
    async def result(self) -> Union[None, dict]:
        """
        异步获取请求结果 
        
        __result 用来暂存数据 参数不变时获取结果不变
        """
        if self.__result is None:
            self.__result = await request(self)
        return self.__result

    @property
    def sync_result(self):
        """
        同步获取请求结果
        """
        return sync(self.result)

    def update_data(self, **kwargs):
        """
        毫无亮点的更新 data
        """
        self.data.update(kwargs)
        self.__result = None
        return self

    def update_params(self, **kwargs):
        """
        毫无亮点的更新 params
        """
        self.params.update(kwargs)
        self.__result = None
        return self

    def update(self, **kwargs):
        """
        毫无亮点的自动选择更新
        """
        if self.method == "GET":
            return self.update_params(**kwargs)
        else:
            return self.update_data(**kwargs)

    @classmethod
    def from_file(cls, path: str):
        """
        以 json 文件生成对象

        Args:
            path (str): 例如 user.info.info
        
        Returns:
            api (Api): 从文件中读取的 api 信息
        """
        path_list = path.split(".")
        api = get_api(path_list.pop(0))
        for key in path_list:
            api = api.get(key)
        return cls(**api)


async def check_valid(credential: Credential) -> bool:
    """
    检查 cookies 是否有效

    Returns:
        bool: cookies 是否有效
    """
    data = await get_nav(credential)
    return data["isLogin"]


async def get_nav(credential: Union[Credential, None] = None):
    """
    获取导航

    Returns:
        dict: 账号相关信息
    """
    return await Api(credential=credential, **get_api("credential")["valid"]).result


async def get_mixin_key() -> str:
    """
    获取混合密钥
    
    Returns:
        str: 新获取的密钥
    """
    data = await get_nav()
    wbi_img: Dict[str, str] = data["wbi_img"]
    split = lambda key: wbi_img.get(key).split("/")[-1].split(".")[0]
    ae = split("img_url") + split("sub_url")
    oe = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
          37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
    le = reduce(lambda s, i: s + (ae[i] if i < len(ae) else ""), oe, "")
    return le[:32]


def enc_wbi(params: dict, mixin_key: str):
    """
    更新请求参数

    Args:
        params (dict): 原请求参数

        mixin_key (str): 混合密钥
    """
    params["wts"] = int(time.time())
    keys = sorted(filter(lambda k: k != "w_rid", params.keys()))
    Ae = "&".join(f"{key}={params[key]}" for key in keys)
    w_rid = hashlib.md5(
        (Ae + mixin_key).encode(encoding="utf-8")
    ).hexdigest()
    params["w_rid"] = w_rid


@atexit.register
def __clean() -> None:
    """
    程序退出清理操作。
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return

    async def __clean_task():
        await __session_pool[loop].close()

    if loop.is_closed():
        loop.run_until_complete(__clean_task())
    else:
        loop.create_task(__clean_task())


async def request_old(
    method: str,
    url: str,
    params: Union[dict, None] = None,
    data: Any = None,
    credential: Union[Credential, None] = None,
    no_csrf: bool = False,
    json_body: bool = False,
    **kwargs,
) -> Any:
    """
    向接口发送请求。

    Args:
        method     (str)                 : 请求方法。
        url        (str)                 : 请求 URL。
        params     (dict, optional)      : 请求参数。
        data       (Any, optional)       : 请求载荷。
        credential (Credential, optional): Credential 类。
        no_csrf    (bool, optional)      : 不要自动添加 CSRF。
        json_body  (bool, optional)      : 载荷是否为 JSON

    Returns:
        接口未返回数据时，返回 None，否则返回该接口提供的 data 或 result 字段的数据。
    """
    if credential is None:
        credential = Credential()

    method = method.upper()
    # 请求为非 GET 且 no_csrf 不为 True 时要求 bili_jct
    if method != "GET" and not no_csrf:
        credential.raise_for_no_bili_jct()

    # 使用 Referer 和 UA 请求头以绕过反爬虫机制
    DEFAULT_HEADERS = {
        "Referer": "https://www.bilibili.com",
        "User-Agent": "Mozilla/5.0",
    }
    headers = DEFAULT_HEADERS

    if params is None:
        params = {}

    # 处理 wbi 鉴权
    # 为什么tmd api 信息不传入而是直接传入 url
    if "wbi" in url:  # 只能暂时这么判断了
        global wbi_mixin_key
        if wbi_mixin_key == "":
            wbi_mixin_key = await get_mixin_key()
        enc_wbi(params, wbi_mixin_key)

    # 自动添加 csrf
    if not no_csrf and method in ["POST", "DELETE", "PATCH"]:
        if data is None:
            data = {}
        data["csrf"] = credential.bili_jct
        data["csrf_token"] = credential.bili_jct

    # jsonp

    if params.get("jsonp", "") == "jsonp":
        params["callback"] = "callback"

    cookies = credential.get_cookies()
    cookies["buvid3"] = str(uuid.uuid1())
    cookies["Domain"] = ".bilibili.com"

    config = {
        "method": method,
        "url": url,
        "params": params,
        "data": data,
        "headers": headers,
        "cookies": cookies,
    }

    config.update(kwargs)

    if json_body:
        config["headers"]["Content-Type"] = "application/json"
        config["data"] = json.dumps(config["data"])

    # config["ssl"] = False

    # config["verify_ssl"] = False
    # config["ssl"] = False

    session = get_session()

    if True:  # try:
        resp = await session.request(**config)
    # except Exception :
    #    raise httpx.ConnectError("连接出错。")

    # 检查响应头 Content-Length
    content_length = resp.headers.get("content-length")
    if content_length and int(content_length) == 0:
        return None

    # 检查响应头 Content-Type
    content_type = resp.headers.get("content-type")

    # 不是 application/json
    # if content_type.lower().index("application/json") == -1:
    #     raise ResponseException("响应不是 application/json 类型")

    raw_data = resp.text
    resp_data: dict

    if "callback" in params:
        # JSONP 请求
        resp_data = json.loads(re.match("^.*?({.*}).*$", raw_data, re.S).group(1))  # type: ignore
    else:
        # JSON
        resp_data = json.loads(raw_data)

    # 检查 code
    code = resp_data.get("code", None)

    if code is None:
        raise ResponseCodeException(-1, "API 返回数据未含 code 字段", resp_data)
    if code != 0:
        msg = resp_data.get("msg", None)
        if msg is None:
            msg = resp_data.get("message", None)
        if msg is None:
            msg = "接口未返回错误信息"
        raise ResponseCodeException(code, msg, resp_data)

    real_data = resp_data.get("data", None)
    if real_data is None:
        real_data = resp_data.get("result", None)
    return real_data


def get_session() -> httpx.AsyncClient:
    """
    获取当前模块的 httpx.AsyncClient 对象，用于自定义请求

    Returns:
        httpx.AsyncClient
    """
    global __session_pool, last_proxy
    loop = asyncio.get_event_loop()
    session = __session_pool.get(loop, None)
    if session is None or last_proxy != settings.proxy:
        if settings.proxy != "":
            last_proxy = settings.proxy
            proxies = {"all://": settings.proxy}
            session = httpx.AsyncClient(proxies=proxies, timeout=settings.timeout)  # type: ignore
        else:
            last_proxy = ""
            session = httpx.AsyncClient(timeout=settings.timeout)
        __session_pool[loop] = session

    return session


def set_session(session: httpx.AsyncClient) -> None:
    """
    用户手动设置 Session

    Args:
        session (httpx.AsyncClient):  httpx.AsyncClient 实例。
    """
    loop = asyncio.get_event_loop()
    __session_pool[loop] = session


def rollback(func):
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except AttributeError:
            return await request_old(*args, **kwargs)
    return wrapper


def retry(times: int = 3):
    """
    重试装饰器

    Args:
        times (int): 最大重试次数 默认 3 次 负数则一直重试直到成功

    Returns:
        Any: 原函数调用结果
    """
    def wrapper(func: Coroutine):
        async def req(*args, **kwargs):
            nonlocal times
            while times != 0:
                times -= 1
                try:
                    result = await func(*args, **kwargs)
                    return result
                except ResponseCodeException as e:
                    # -403 时尝试重新获取 wbi_mixin_key 可能过期了
                    if e.code == -403 and times != 0:
                        global wbi_mixin_key
                        wbi_mixin_key = ""
                        continue
                    else:
                        # 不是 -403 错误或者重试次数达到了报最后一次错
                        raise e
        return req

    if isAsync(times):
        # 防呆不防傻 防止有人 @retry() 不打括号
        func = times
        times = 3
        return wrapper(func)

    return wrapper


@rollback
@retry()
async def request(api: Api, url: str = "", params: dict = None, **kwargs) -> Any:
    """
    向接口发送请求。

    Args:
        api (Api): 请求Api信息。
        url, params: 这两个参数是为了通过 Conventional Commits 写的，最后使用的时候(指完全取代老的之后)可以去掉。

    Returns:
        接口未返回数据时，返回 None，否则返回该接口提供的 data 或 result 字段的数据。
    """
    # 请求为非 GET 且 no_csrf 不为 True 时要求 bili_jct
    if api.method != "GET" and not api.no_csrf:
        api.credential.raise_for_no_bili_jct()
    
    if settings.request_log:
        print(f"Request {api}")
    
    # jsonp
    if api.params.get("jsonp") == "jsonp":
        api.params["callback"] = "callback"

    if api.wbi:
        global wbi_mixin_key
        if wbi_mixin_key == "":
            wbi_mixin_key = await get_mixin_key()
        enc_wbi(api.params, wbi_mixin_key)

    # 自动添加 csrf
    if not api.no_csrf and api.method in ["POST", "DELETE", "PATCH"]:
        api.data["csrf"] = api.credential.bili_jct
        api.data["csrf_token"] = api.credential.bili_jct

    cookies = api.credential.get_cookies()
    cookies["buvid3"] = str(uuid.uuid1())
    cookies["Domain"] = ".bilibili.com"

    config = {
        "method": api.method,
        "url": api.url,
        "params": api.params,
        "data": api.data,
        "headers": HEADERS,
        "cookies": cookies,
    }
    config.update(kwargs)

    if api.json_body:
        config["headers"]["Content-Type"] = "application/json"
        config["data"] = json.dumps(config["data"])

    session = get_session()

    resp = await session.request(**config)

    # 检查响应头 Content-Length
    content_length = resp.headers.get("content-length")
    if content_length and int(content_length) == 0:
        return None

    if "callback" in api.params:
        # JSONP 请求
        resp_data: dict = json.loads(re.match("^.*?({.*}).*$", resp.text, re.S).group(1))
    else:
        # JSON
        resp_data: dict = json.loads(resp.text)

    # 检查 code
    if not api.ignore_code:
        code = resp_data.get("code")

        if code is None:
            raise ResponseCodeException(-1, "API 返回数据未含 code 字段", resp_data)
        if code != 0:
            msg = resp_data.get("msg")
            if msg is None:
                msg = resp_data.get("message")
            if msg is None:
                msg = "接口未返回错误信息"
            raise ResponseCodeException(code, msg, resp_data)

    real_data = resp_data.get("data")
    if real_data is None:
        real_data = resp_data.get("result")
    return real_data