"""命令行入口。

设计原则：不带参数就能跑完整流程，参数全部可选。
  jr            抓取 + 分析 + 出报告
  jr 抓 / crawl  只抓取
  jr 报告 / report 只分析（用库里已有数据）
  jr 看 / stats   看库里有多少岗位
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .spiders import all_slugs
from .store import Store

DEFAULT_DB = "data/jobs.db"
DEFAULT_TRACKS = ["backend", "ai"]
# 默认抓校招+实习。要社招得显式 --type social
DEFAULT_RECRUIT = ["campus", "intern"]

# 用 SUPPRESS 注册、事后回填默认值：这样同一个参数在主命令和子命令上
# 都能识别，且放在子命令前后都生效（否则 argparse 会用子命令的默认值
# 覆盖掉你在前面写的值）。
COMMON_DEFAULTS = {
    "db": DEFAULT_DB,
    "verbose": False,
    "max_pages": 30,
    "interval": 1.5,
    "limit": 150,
    "batch_size": 15,
    "out": "reports/report.md",
    "tracks": DEFAULT_TRACKS,
    "companies": None,
    "type": DEFAULT_RECRUIT,
}


def _add_common(ap: argparse.ArgumentParser) -> None:
    S = argparse.SUPPRESS
    ap.add_argument("--db", default=S, help=S)
    ap.add_argument("-v", "--verbose", action="store_true", default=S,
                    help="打印调试日志")
    ap.add_argument("--max-pages", type=int, default=S,
                    help="每家最多翻几页（默认 30）")
    ap.add_argument("--interval", type=float, default=S, help=S)
    ap.add_argument("--limit", type=int, default=S,
                    help="送进大模型的岗位数上限（默认 150，越大越贵）")
    ap.add_argument("--batch-size", type=int, default=S, help=S)
    ap.add_argument("--out", default=S, help="报告输出路径")
    ap.add_argument("--tracks", nargs="*", default=S,
                    choices=["backend", "ai", "other"], help="分析哪些方向")
    ap.add_argument("--companies", nargs="*", default=S,
                    help="限定公司")
    ap.add_argument("--type", nargs="*", default=S,
                    choices=["campus", "intern", "social"],
                    help="招聘类型：campus 校招 / intern 实习 / social 社招"
                         "（默认 campus intern）")


def _setup_log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
    )
    # httpx 每个请求都打一行 INFO，太吵
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _print_counts(counts: dict) -> None:
    print(f"\n库内共 {counts['total']} 个岗位")
    label = {"backend": "后端/服务端", "ai": "算法/AI", "other": "其他"}
    for k, v in sorted(counts["by_track"].items(), key=lambda x: -x[1]):
        print(f"  {label.get(k, k):<12} {v:>5}")
    rec = counts.get("by_recruit") or {}
    if rec:
        rlabel = {"social": "社招", "campus": "校招", "intern": "实习"}
        print("  " + "-" * 18)
        for k, v in sorted(rec.items(), key=lambda x: -x[1]):
            print(f"  {rlabel.get(k, k):<12} {v:>5}")
    print("  " + "-" * 18)
    for k, v in sorted(counts["by_company"].items(), key=lambda x: -x[1]):
        print(f"  {k:<12} {v:>5}")


def _crawl_printer(ev) -> None:
    """把 service 的 Event 翻译回 CLI 原有的那几行输出。"""
    if ev.kind == "spider_start":
        print(f"\n抓取 {ev.message} ...")


def do_crawl(db: str, companies: list[str] | None, max_pages: int,
             interval: float, recruit_types: list[str] | None = None) -> dict:
    from .service import run_crawl

    res = run_crawl(db, companies=companies, max_pages=max_pages,
                    interval=interval, recruit_types=recruit_types,
                    on_event=_crawl_printer)
    for c in res.companies:
        if c.error:
            logging.error("  %s 采集中断: %s", c.company, c.error)
        print(f"  {c.company}: 抓到 {c.fetched} 条，新增 {c.inserted} 条")
    print(f"\n本轮新增 {res.total_inserted} 条")
    return res.counts


def do_analyze(db: str, tracks: list[str], companies: list[str] | None,
               limit: int, batch_size: int, out: str,
               recruit_types: list[str] | None = None) -> int:
    from .service import AnalyzeStatus, run_analyze

    def printer(ev) -> None:
        if ev.kind == "phase" and ev.phase == "extract":
            print(f"\n{ev.message} ...")
        elif ev.kind == "phase" and ev.phase == "report":
            print("汇总生成报告 ...")

    res = run_analyze(db, tracks=tracks, companies=companies, limit=limit,
                      batch_size=batch_size, out=Path(out),
                      recruit_types=recruit_types, on_event=printer)

    if res.status is AnalyzeStatus.NO_JOBS:
        print("库里没有匹配的岗位，先跑 `jr 抓`。", file=sys.stderr)
        return 1
    if res.status is AnalyzeStatus.LLM_CONFIG:
        print(f"\n大模型配置有问题: {res.error}", file=sys.stderr)
        return 2
    if res.status is AnalyzeStatus.EXTRACT_FAILED:
        print("技能抽取全部失败，检查网络和 .env 里的密钥。", file=sys.stderr)
        return 3
    if res.status is AnalyzeStatus.REPORT_FAILED:
        print(f"\n生成报告失败：{res.error}", file=sys.stderr)
        print(f"但频次数据已存好：{res.stats_path}", file=sys.stderr)
        print("抽取结果也已存库，补足额度后重跑 `jr 报告` 会直接复用。",
              file=sys.stderr)
        return 4

    if res.status is AnalyzeStatus.PARTIAL:
        print(f"  注意：{res.jobs_selected} 个岗位里成功抽取 "
              f"{res.extracted_ok} 个，其余已跳过（结果已存库，重跑会复用）")

    print("\n" + "=" * 60)
    print(res.markdown)
    print("=" * 60)
    print(f"\n报告已存：{res.out_path}")
    print(f"频次数据：{res.stats_path}")
    return 0


def cmd_go(args) -> int:
    """默认流程：抓 + 分析 + 出报告。"""
    counts = do_crawl(args.db, None, args.max_pages, args.interval,
                      recruit_types=args.type)
    _print_counts(counts)
    return do_analyze(args.db, DEFAULT_TRACKS, None, args.limit,
                      args.batch_size, args.out, recruit_types=args.type)


def cmd_crawl(args) -> int:
    counts = do_crawl(args.db, args.companies, args.max_pages, args.interval,
                      recruit_types=args.type)
    _print_counts(counts)
    print("\n下一步：jr 报告")
    return 0


def cmd_report(args) -> int:
    return do_analyze(args.db, args.tracks, args.companies, args.limit,
                      args.batch_size, args.out, recruit_types=args.type)


def cmd_stats(args) -> int:
    store = Store(args.db)
    _print_counts(store.counts())
    store.close()
    return 0


def cmd_serve(args) -> int:
    from .serve import serve
    return serve(port=getattr(args, "port", None))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="jr",
        description="抓大厂招聘岗位，用大模型分析行业缺什么人才",
        epilog=f"不带参数直接跑 `jr` 就是：抓取 + 分析 + 出报告\n"
               f"可选公司：{' '.join(all_slugs())}",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_common(p)
    p.set_defaults(func=cmd_go)

    sub = p.add_subparsers(dest="cmd")
    # 中英双写，记不住哪个都能用
    for names, fn, helptext in [
        (["抓", "crawl"], cmd_crawl, "只抓取岗位，存进本地库"),
        (["报告", "report", "analyze"], cmd_report, "只分析（用库里已有数据）"),
        (["看", "stats"], cmd_stats, "看库里现在有多少岗位"),
        (["服务", "serve", "web"], cmd_serve, "启动网页版（仪表盘/报告/任务）"),
    ]:
        s = sub.add_parser(names[0], aliases=names[1:], help=helptext,
                           description=helptext)
        _add_common(s)          # 子命令也收同一批参数，位置随意
        if fn is cmd_serve:
            s.add_argument("--port", type=int, default=None, help="端口，默认 8787")
        s.set_defaults(func=fn)

    args = p.parse_args(argv)
    for key, val in COMMON_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, val)
    _setup_log(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。已抓到的数据都存好了，重跑不会重复。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
