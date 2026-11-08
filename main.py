"""程序入口：加载配置、注册飞书消息事件，并管理后台线程和 HTTP 连接。"""

import argparse
from dataclasses import replace
import json
import logging
import sys

import lark_oapi as lark

from summary_bot.config import Config
from summary_bot.feishu import FeishuError, FeishuGateway
from summary_bot.llm import AnthropicMessagesClient
from summary_bot.service import SummaryBot
from summary_bot.store import JobStore


def main() -> int:
    """解析命令行并执行配置检查或启动机器人。

    不带参数时，创建飞书网关、模型客户端和持久化队列，启动后台工作线程，
    再通过飞书 SDK 的长连接阻塞接收事件。传入 ``--check-config`` 时仅检查
    配置格式，不创建业务客户端、不访问网络，也不创建任务数据库。

    返回：
        配置检查通过或正常退出时返回 0；配置缺失或无效时返回 2。
        命令行帮助和非法命令行参数由 argparse 自行处理退出。

    运行行为：
        Ctrl+C 会触发收尾，等待当前后台任务结束后关闭本程序创建的模型连接。
        尚未开始的任务保留在数据库中，供下一次启动恢复。
    """
    parser = argparse.ArgumentParser(description="飞书「消息总结」机器人")
    parser.add_argument("--check-config", action="store_true", help="仅验证配置，不连接飞书或调用模型")
    args = parser.parse_args()
    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # HTTP 调试日志可能包含内部服务地址，不随业务 DEBUG 级别启用。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if args.check_config:
        print("配置检查通过（尚未验证凭据、模型可用性和飞书权限）。")
        return 0
    gateway = FeishuGateway(config)
    if not config.bot_open_id:
        try:
            config = replace(config, bot_open_id=gateway.get_bot_open_id())
        except FeishuError as exc:
            logging.error("无法获取机器人身份：%s", exc)
            return 2
        gateway = FeishuGateway(config, gateway.client)
    llm = AnthropicMessagesClient(config)
    store = JobStore(
        config.state_db_path, max_retries=config.job_max_retries,
        retry_base_seconds=config.job_retry_base_seconds,
        retry_max_seconds=config.job_retry_max_seconds,
    )
    bot = SummaryBot(config, gateway, llm, store)

    def on_message(data) -> None:
        """接收 SDK 解码后的消息事件，将事件正文转换为字典后提交任务。

        参数：
            data：飞书 ``P2ImMessageReceiveV1`` 事件对象；只传递其中的
                ``event``，事件头不参与业务消息去重。

        返回：
            None。过滤和数据库写入由 ``bot.submit`` 完成；历史查询、
            模型调用和回复均留给后台线程，以便及时结束事件回调。

        异常：
            序列化或任务落盘异常会向 SDK 传播，不在回调中伪装处理成功。
        """
        # 与 echo_bot 一样订阅 v2 消息事件，仅做过滤与落盘，立即返回给 SDK。
        bot.submit(json.loads(lark.JSON.marshal(data.event)))

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message).build()
    )
    ws_client = lark.ws.Client(
        config.app_id, config.app_secret,
        event_handler=handler, log_level=lark.LogLevel.WARNING,
    )
    bot.start()
    logging.info("消息总结已启动，仅响应群内 @，使用引用合并转发记录的总结方式")
    try:
        ws_client.start()
    except KeyboardInterrupt:
        logging.info("正在停止，等待当前任务结束")
    finally:
        bot.close()
        llm.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
