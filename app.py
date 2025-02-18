import websocket
import json
import time
import threading
import requests
import telebot
from flask import Flask, render_template, jsonify, request

# Telegram 机器人配置
TELEGRAM_API_TOKEN = '7796075418:AAHM_WukbWb3UDdLi-ktEtIiYsccQTghTRI'  # 替换为你的 token
CHAT_ID = 6209211106  # 确保为整数

# 创建 TeleBot 实例
bot = telebot.TeleBot(TELEGRAM_API_TOKEN)


def send_telegram_message(message):
    try:
        bot.send_message(CHAT_ID, message)
        print("消息发送成功:", message)
    except Exception as e:
        print("发送消息异常:", e)


app = Flask(__name__)

# WebSocket与REST API配置
BINANCE_WS_URL = "wss://fstream.binance.com/"  # 此处使用合约WebSocket服务器
BINANCE_API_URL = "https://api.binance.com/api/v3/ticker/price"
STREAMS_PER_CONNECTION = 200

# 全局数据缓存
price_history = {}  # { symbol: [(timestamp, price), ...] }
symbols = []        # 所有币种列表
ws_connections = []  # WebSocket 连接列表
last_alert_time = {}  # { symbol: timestamp } 用于限制警报频率
ALERT_INTERVAL = 60  # 同一币种每60秒内只发一次警报


def on_ping(ws, message):
    print("收到Ping消息，发送Pong")
    ws.send('pong')


def on_pong(ws, message):
    print("收到Pong消息，WebSocket连接仍然活跃")


def on_message(ws, message):
    try:
        data = json.loads(message)
    except Exception as e:
        print("JSON解析错误:", e)
        return

    # 过滤订阅确认消息
    if "result" in data and data["result"] is None:
        print("订阅成功，等待市场数据...")
        return

    if "stream" in data and "data" in data:
        payload = data["data"]
        symbol = payload.get("s")
        try:
            price = float(payload.get("c", 0))
        except Exception as e:
            print(f"解析 {symbol} 价格错误:", e)
            return
        timestamp = time.time()

        # 打印当前价格（调试用）
        # print(f"[{symbol}] 当前价格: {price}")

        # 更新历史价格
        if symbol not in price_history:
            price_history[symbol] = []
        price_history[symbol].append((timestamp, price))
        # 保留最近15分钟内的数据（900秒）
        price_history[symbol] = [
            (t, p) for t, p in price_history[symbol] if timestamp - t <= 900]

        # print(f"[{symbol}] 最新价格: {price}, 历史数据点数: {len(price_history[symbol])}")
    else:
        print("数据格式不符合预期:", data)


def on_error(ws, error):
    print(f"WebSocket 错误: {error}")


def on_close(ws, close_status_code, close_msg):
    print(f"WebSocket 连接关闭，状态码: {close_status_code}, 消息: {close_msg}")
    print("等待 5 秒后尝试重连...")
    time.sleep(5)
    start_websocket_connections()


def on_open(ws):
    print(f"WebSocket 连接成功: {ws.url}")
    # 组合 streams 模式下，服务器会自动推送数据


def start_websocket_connections():
    global ws_connections
    # 关闭旧连接
    for ws in ws_connections:
        try:
            ws.close()
        except Exception as e:
            print("关闭连接错误:", e)
    ws_connections = []

    if not symbols:
        print("symbols 为空，无法建立 WebSocket 连接")
        return

    # 拆分订阅，每个连接最多订阅 STREAMS_PER_CONNECTION 个币种
    for i in range(0, len(symbols), STREAMS_PER_CONNECTION):
        subset = symbols[i:i + STREAMS_PER_CONNECTION]
        # 拼接多个 stream，格式：<symbol>@ticker/<symbol2>@ticker/...
        # 注意：合约数据的订阅依然使用小写 stream 名称
        streams = "/".join([f"{symbol.lower()}@ticker" for symbol in subset])
        ws_url = BINANCE_WS_URL + "stream?streams=" + streams
        print("建立 WebSocket 连接:", ws_url)
        ws = websocket.WebSocketApp(
            ws_url,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
            on_open=on_open
        )
        ws_connections.append(ws)

        # 使用线程定期发送 ping 消息
        def send_ping(ws_instance):
            while ws_instance.sock and ws_instance.sock.connected:
                time.sleep(30)  # 每30秒发送一次 ping
                try:
                    ws_instance.ping("ping")
                    print("发送 Ping 消息")
                except Exception as e:
                    print("发送 ping 错误:", e)
                    break

        threading.Thread(target=send_ping, args=(ws,), daemon=True).start()
        threading.Thread(target=ws.run_forever, daemon=True).start()


def fetch_symbols():
    global symbols
    try:
        response = requests.get(BINANCE_API_URL).json()
        # 保持币种大写
        symbols = [item["symbol"]
                   for item in response if item["symbol"].endswith("USDT")]
        print(f"监控 {len(symbols)} 个币种：", symbols)
    except Exception as e:
        print("fetch_symbols 错误:", e)


def refresh_symbols():
    while True:
        time.sleep(86400)  # 每24小时刷新一次币种列表
        fetch_symbols()
        start_websocket_connections()

# 修改后的计算函数：选择历史中最接近 (now - time_range_seconds) 的数据作为基准


def calculate_change(symbol, time_range_seconds):
    now = time.time()
    history = price_history.get(symbol, [])
    if not history:
        return None

    # 过滤出所有时间点 <= now - time_range_seconds
    valid_points = [(t, p)
                    for t, p in history if t <= now - time_range_seconds]
    if not valid_points:
        print(f"数据不足以计算 {time_range_seconds//60} 分钟涨跌幅 [{symbol}]")
        return None

    # 选择最接近 now - time_range_seconds 的数据点（最大 t）
    base_t, base_price = max(valid_points, key=lambda x: x[0])
    current_price = history[-1][1]
    change = ((current_price - base_price) / base_price) * 100
    # print(
    # f"计算涨跌幅 [{symbol}] - 基准时间: {base_t}, 基准价格: {base_price}, 当前价格: {current_price}, 涨跌幅: {change:.2f}%")
    return change

# 计算涨跌幅并发送警报（同一币种一分钟内只发一次警报）


def calculate_change_and_alert(symbol, time_range_seconds, gain_change_threshold, loss_change_threshold):
    now = time.time()
    change = calculate_change(symbol, time_range_seconds)
    if change is None:
        return None

    # 判断是否满足警报条件
    if (change >= gain_change_threshold or change <= -loss_change_threshold):
        # 检查上次是否发送过警报
        if symbol in last_alert_time and now - last_alert_time[symbol] < ALERT_INTERVAL:
            print(f"[{symbol}] 警报已在 {ALERT_INTERVAL} 秒内发送，跳过")
        else:
            if change >= gain_change_threshold:
                message = f"警报: {symbol} 在过去{time_range_seconds // 60}分钟内涨幅达到{change:.2f}% (超过 {gain_change_threshold}%)"
                print(message)
                send_telegram_message(message)
            elif change <= -loss_change_threshold:
                message = f"警报: {symbol} 在过去{time_range_seconds // 60}分钟内跌幅达到{change:.2f}% (超过 {loss_change_threshold}%)"
                print(message)
                send_telegram_message(message)
            last_alert_time[symbol] = now
    return change

# API 接口：处理前端参数并返回涨跌幅数据，同时触发警报


@app.route('/crypto-prices', methods=['GET'])
def crypto_prices_api():
    try:
        gain_time_range = int(request.args.get("gain_time_range", 5))  # 单位：分钟
        gain_change_threshold = float(
            request.args.get("gain_change_threshold", 2))
        loss_time_range = int(request.args.get(
            "loss_time_range", 5))    # 单位：分钟
        loss_change_threshold = float(
            request.args.get("loss_change_threshold", 2))
    except Exception as e:
        return jsonify({"error": "参数错误"}), 400

    gain_seconds = gain_time_range * 60
    loss_seconds = loss_time_range * 60

    high_gains = []
    low_losses = []

    # 遍历所有币种，计算涨跌幅并发送警报
    for symbol in symbols:
        change = calculate_change_and_alert(
            symbol, gain_seconds, gain_change_threshold, loss_change_threshold)
        if change is not None:
            if change >= gain_change_threshold:
                high_gains.append([symbol, change, f"{gain_time_range}分钟"])
            if change <= -loss_change_threshold:
                low_losses.append([symbol, change, f"{loss_time_range}分钟"])

    high_gains.sort(key=lambda x: x[1], reverse=True)
    low_losses.sort(key=lambda x: x[1])

    result = {
        "time_range": f"{gain_time_range}分钟",
        "high_gains": high_gains,
        "low_losses": low_losses
    }
    return jsonify(result)


@app.route('/')
def index():
    return render_template("index.html")


def run_flask():
    app.run(host="127.0.0.1", port=5000)


def run():
    fetch_symbols()
    start_websocket_connections()
    threading.Thread(target=refresh_symbols, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("捕获到键盘中断，退出程序")
        for ws in ws_connections:
            try:
                if ws.sock and ws.sock.connected:
                    ws.close()
            except Exception as e:
                print("关闭 ws 时错误:", e)


if __name__ == "__main__":
    run()
