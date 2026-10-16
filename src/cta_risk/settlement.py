from __future__ import annotations

from html import escape


def render_daily_report(payload: dict) -> tuple[str, str]:
    """把清算结果渲染成文本日报和 HTML 日报。"""
    lines = [
        f"风控日报  {payload['trading_day']}",
        f"生成时间  {payload['generated_at']}",
        "",
        (
            f"公司合计  权益 {payload['firm']['equity']}  "
            f"当日净盈亏 {payload['firm']['daily_pnl']}  "
            f"保证金 {payload['firm']['margin']}  "
            f"可用 {payload['firm']['available']}"
        ),
        "",
    ]
    for account in payload["accounts"]:
        lines.append(
            f"[{account['account_id']}] {account['name']}  "
            f"状态 {'熔断' if account['halted'] else '正常'}"
        )
        lines.append(
            f"  上日权益 {account['prev_equity']}  平仓盯市 {account['close_pnl']}  "
            f"持仓盯市 {account['position_pnl']}  手续费 {account['commission']}"
        )
        lines.append(
            f"  盯市盈亏 {account['mtm_pnl']}  当日净盈亏 {account['daily_pnl']}  "
            f"结算权益 {account['equity']}"
        )
        lines.append(
            f"  保证金 {account['margin']}  可用 {account['available']}  "
            f"风险度 {account['margin_ratio']}"
        )
        lines.append("  持仓")
        if not account["positions"]:
            lines.append("    （空仓）")
        for pos in account["positions"]:
            lines.append(
                f"    {pos['instrument']} {pos['side']} {pos['qty']}手  "
                f"基准 {pos['basis']}  结算 {pos['settle']}  "
                f"盯市 {pos['pnl']}  保证金 {pos['margin']}"
            )
        lines.append("")
    lines.append("当日风控事件")
    if not payload["events"]:
        lines.append("  （无）")
    for event in payload["events"]:
        lines.append(f"  {event['event_id']} {event['level']} {event['account_id']} {event['message']}")
    lines.append("")
    lines.append("当日拒单")
    if not payload["rejects"]:
        lines.append("  （无）")
    for reject in payload["rejects"]:
        lines.append(
            f"  {reject['account_id']} {reject['instrument']} {reject['side']} {reject['offset']} "
            f"{reject['qty']}手  {reject['reason']}"
        )
    text = "\n".join(lines) + "\n"

    account_blocks = []
    for account in payload["accounts"]:
        rows = "".join(
            "<tr>"
            f"<td>{escape(pos['instrument'])}</td>"
            f"<td>{escape(pos['side'])}</td>"
            f"<td>{pos['qty']}</td>"
            f"<td>{escape(pos['basis'])}</td>"
            f"<td>{escape(pos['settle'])}</td>"
            f"<td>{escape(pos['pnl'])}</td>"
            f"<td>{escape(pos['margin'])}</td>"
            "</tr>"
            for pos in account["positions"]
        ) or "<tr><td colspan='7'>空仓</td></tr>"
        account_blocks.append(
            f"""
            <section>
              <h2>{escape(account['account_id'])} {escape(account['name'])}</h2>
              <p>上日权益 {escape(account['prev_equity'])}
                 平仓盯市 {escape(account['close_pnl'])}
                 持仓盯市 {escape(account['position_pnl'])}
                 手续费 {escape(account['commission'])}</p>
              <p>盯市盈亏 {escape(account['mtm_pnl'])}
                 当日净盈亏 {escape(account['daily_pnl'])}
                 结算权益 {escape(account['equity'])}
                 保证金 {escape(account['margin'])}
                 可用 {escape(account['available'])}
                 风险度 {escape(account['margin_ratio'])}
                 熔断 {"是" if account["halted"] else "否"}</p>
              <table>
                <thead><tr><th>合约</th><th>方向</th><th>手数</th><th>盯市基准</th><th>结算价</th><th>盯市盈亏</th><th>保证金</th></tr></thead>
                <tbody>{rows}</tbody>
              </table>
            </section>
            """
        )
    event_rows = "".join(
        f"<li>{escape(event['event_id'])} {escape(event['level'])} {escape(event['account_id'])} {escape(event['message'])}</li>"
        for event in payload["events"]
    ) or "<li>无</li>"
    reject_rows = "".join(
        f"<li>{escape(item['account_id'])} {escape(item['instrument'])} {escape(item['reason'])}</li>"
        for item in payload["rejects"]
    ) or "<li>无</li>"
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>风控日报 {escape(payload['trading_day'])}</title>
  <style>
    body {{ font-family: "Noto Sans CJK SC", "Source Han Sans SC", sans-serif; margin: 32px; color: #1c2430; }}
    h1 {{ margin-bottom: 0; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
    th, td {{ border: 1px solid #d5dbe3; padding: 6px 8px; text-align: right; }}
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) {{ text-align: left; }}
    section {{ margin-top: 28px; }}
  </style>
</head>
<body>
  <h1>风控日报 {escape(payload['trading_day'])}</h1>
  <p>生成时间 {escape(payload['generated_at'])}</p>
  <p>公司合计 权益 {escape(payload['firm']['equity'])}
     当日净盈亏 {escape(payload['firm']['daily_pnl'])}
     保证金 {escape(payload['firm']['margin'])}
     可用 {escape(payload['firm']['available'])}</p>
  {''.join(account_blocks)}
  <section><h2>当日风控事件</h2><ul>{event_rows}</ul></section>
  <section><h2>当日拒单</h2><ul>{reject_rows}</ul></section>
</body>
</html>
"""
    return text, html
