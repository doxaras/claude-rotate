"""Generate the claude-rotate architecture diagram (mingrammer/diagrams)."""
from pathlib import Path

from diagrams import Cluster, Diagram, Edge
from diagrams.onprem.ci import GithubActions
from diagrams.onprem.client import Client, User
from diagrams.onprem.compute import Server
from diagrams.generic.storage import Storage
from diagrams.programming.language import Python

graph_attr = {
    "bgcolor": "white",
    "pad": "0.4",
    "splines": "spline",
    "fontsize": "13",
    "dpi": "160",
    "ranksep": "1.1",
    "nodesep": "0.6",
}
node_attr = {"fontsize": "12"}
edge_attr = {"fontsize": "11"}

with Diagram(
    "",  # no title — README section heading does that job
    filename=str(Path(__file__).parent / "architecture"),
    outformat="png",
    direction="LR",
    show=False,
    graph_attr=graph_attr,
    node_attr=node_attr,
    edge_attr=edge_attr,
):
    with Cluster("your devices"):
        laptop = Client("laptop")
        desktop = Client("desktop")
        ci = GithubActions("ci box")

    with Cluster("claude-rotate  ·  your server"):
        tokens = Storage("tokens/*.token\none ~1-yr setup-token\nper subscription")
        proxy = Python("rotator.py\ndevice auth → token swap\nconsume-first rotation\nquota 429 → rotate · burst 429 → pace\nall spent → hold until reset")
        audit = Storage("audit.jsonl + state.json\nusage & $-equiv per device/model\n→ /rotate/panel")
        # keep the stores stacked beside the proxy without stretching the rank
        proxy - Edge(style="dotted", constraint="false") - tokens
        proxy - Edge(style="dotted", constraint="false") - audit

    anthropic = Server("api.anthropic.com")

    for dev in (laptop, desktop, ci):
        dev >> Edge(label="Bearer <device key>" if dev is desktop else "") >> proxy

    proxy >> Edge(label="Bearer <active account's\nsetup-token>") >> anthropic
    anthropic >> Edge(
        style="dashed", color="darkgreen", constraint="false",
        label="anthropic-ratelimit-unified-*\nexact 5h/7d utilization + resets",
    ) >> proxy
