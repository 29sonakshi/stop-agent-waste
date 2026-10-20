"""
Tiffin — Delivery agent console. Thin UI over agent.py.

All agent logic lives in agent.py, which the test notebook (10_test_agent) also imports,
so what gets tested is what ships. This file is presentation only.
"""
import json

import streamlit as st

import agent
import lakebase

st.set_page_config(page_title="Tiffin | Delivery agent",
                   page_icon="🍱", layout="wide")

# ---------------------------------------------------------------- chrome + styles
# Hide the Streamlit menu/footer so the app reads as a product rather than a notebook.
st.markdown("""
<style>
  #MainMenu, footer {visibility: hidden;}
  .block-container {padding-top: 2.2rem; max-width: 1150px;}
  .tf-head {display:flex; align-items:center; gap:14px; margin-bottom:2px;}
  .tf-name {font-size:26px; font-weight:700; letter-spacing:-.4px; color:#1A1A1A;}
  .tf-goal {font-size:13.5px; color:#6B6B6B; margin:2px 0 20px 2px;}
  .tf-turn{font-size:11px; font-weight:700; letter-spacing:.9px; color:#9A9A9A;
            text-transform:uppercase; margin:22px 0 8px;}
  .tf-pill {display:inline-block; font-size:13px; font-weight:700; color:#fff;
            border-radius:999px; padding:4px 13px;}
  .tf-rec  {font-size:14px; color:#1A1A1A; background:#FFF8F1;
            border-left:3px solid #E8590C; padding:9px 13px; margin:8px 0;}
  .tf-tool {font-size:13px; color:#444; margin-bottom:4px;}
  .tf-tool code {background:#F0EDE9; padding:1px 6px; border-radius:3px;}
  .tf-diff {font-family:ui-monospace,monospace; font-size:12.5px; line-height:1.7;}
  .tf-old  {color:#B02A0E; text-decoration:line-through;}
  .tf-new  {color:#15803D; font-weight:600;}
</style>""", unsafe_allow_html=True)

LOGO = """<svg width="38" height="38" viewBox="0 0 48 48" fill="none">
  <rect x="9" y="19" width="30" height="11" rx="2.5" fill="#E8590C"/>
  <rect x="9" y="31" width="30" height="10" rx="2.5" fill="#1A1A1A"/>
  <rect x="13" y="12" width="22" height="7" rx="2" fill="#F08C00"/>
  <path d="M18 12V9a6 6 0 0 1 12 0v3" stroke="#1A1A1A" stroke-width="2.4"
        stroke-linecap="round" fill="none"/>
</svg>"""

st.markdown(f'<div class="tf-head">{LOGO}'
            f'<span class="tf-name">tiffin</span>'
            f'<span style="color:#C9C4BE;font-size:22px;">|</span>'
            f'<span style="font-size:18px;color:#444;">Delivery agent</span></div>',
            unsafe_allow_html=True)
st.markdown('<div class="tf-goal">Ask this agent anything about a courier &mdash; their '
            'deliveries, which ones are running late, the merchants they pick up from, or a '
            'customer&rsquo;s address. It answers by calling Tiffin&rsquo;s delivery systems '
            'for you.</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------- input
# Quick-pick buttons must render ABOVE the text area: a click reruns the script, and the
# session_state assignment has to happen before the text_area widget is instantiated.
if "req" not in st.session_state:
    st.session_state.req = agent.DEFAULT_REQUEST

PICKS = [("Late deliveries", agent.DEFAULT_REQUEST)]

pcols = st.columns([1, 1, 1, 3])
for col, (label, text) in zip(pcols, PICKS):
    if col.button(label, use_container_width=True):
        st.session_state.req = text

left, right = st.columns([3, 1])
with left:
    request = st.text_area("Dispatcher's question", key="req", height=90)
with right:
    st.write("")
    guard_on = st.toggle("Pre-flight guard", value=True) if hasattr(st, "toggle") \
        else st.checkbox("Pre-flight guard", value=True)
    go = st.button("Run agent", type="primary", use_container_width=True)

PILL = {"coerce": "#1D4ED8", "allow": "#0F766E"}
ICON = {"coerce": "fixed", "allow": "clean"}

# ---------------------------------------------------------------- run
if go:
    body = st.container()
    last_args = {}

    def emit(ev):
        t = ev["type"]
        with body:
            if t == "turn":
                st.markdown(f'<div class="tf-turn">Turn {ev["turn"]}</div>',
                            unsafe_allow_html=True)
            elif t == "proposed":
                last_args.clear(); last_args.update(ev["args"])
                st.markdown(f'<div class="tf-tool">Agent wants to call '
                            f'<code>{ev["tool"]}</code></div>', unsafe_allow_html=True)
                st.code(json.dumps(ev["args"], indent=2), language="json")
            elif t == "guard":
                p = ev["pred"]
                a = p.get("action", "allow")
                extra = (f' &middot; <span style="color:#6B6B6B">{p.get("mismatch_class")}</span>'
                         if a != "allow" and p.get("mismatch_class") else "")
                if p.get("borderline"):
                    extra += (' &middot; <span style="color:#B45309">borderline, '
                              'not rewritten</span>')
                st.markdown(
                    f'<div style="margin:6px 0 2px;">'
                    f'<span class="tf-pill" style="background:{PILL.get(a,"#555")}">'
                    f'{a}</span> '
                    f'<span style="font-size:13px;color:#6B6B6B;">'
                    f'failure probability <b style="color:#1A1A1A">'
                    f'{p.get("failure_probability")}</b>{extra}</span></div>',
                    unsafe_allow_html=True)
                if a != "allow" and p.get("suggested_coercion"):
                    st.markdown(f'<div class="tf-rec"><b>Recommendation</b> &mdash; '
                                f'{p["suggested_coercion"]}</div>', unsafe_allow_html=True)
            elif t == "coerced":
                # show only what changed -- a full JSON re-dump hides the point
                rows = []
                for k, v in ev["args"].items():
                    before = last_args.get(k)
                    if before != v:
                        rows.append(f'{k}<br>&nbsp;&nbsp;<span class="tf-old">'
                                    f'{json.dumps(before)}</span><br>&nbsp;&nbsp;'
                                    f'<span class="tf-new">{json.dumps(v)}</span>')
                st.markdown(f'<div class="tf-rec"><b>Applied</b> &mdash; {ev["note"]}'
                            + (f'<div class="tf-diff" style="margin-top:7px;">'
                               + "<br>".join(rows) + "</div>" if rows else "")
                            + "</div>", unsafe_allow_html=True)
                last_args.clear(); last_args.update(ev["args"])
            elif t == "guard_error":
                st.warning(f"Guard unreachable — failing open, call allowed. "
                           f"({ev['error'][:140]})")
            elif t == "result":
                if ev["ok"]:
                    st.markdown('<span style="color:#0F766E;font-size:13px;">'
                                '&#10003; tool succeeded</span>', unsafe_allow_html=True)
                else:
                    st.markdown(f'<span style="color:#B02A0E;font-size:13px;">'
                                f'&#10007; tool rejected the call &mdash; '
                                f'{ev["result"]}</span>', unsafe_allow_html=True)
                if isinstance(ev["result"], dict):
                    st.code(json.dumps(ev["result"], indent=2), language="json")
            elif t == "final":
                st.markdown('<div class="tf-turn">Answer</div>', unsafe_allow_html=True)
                st.markdown(ev["text"])

    with st.spinner("agent running…"):
        try:
            summary = agent.run_agent(request, guard_on=guard_on, emit=emit,
                                      logger=lakebase)
        except Exception as e:  # noqa: BLE001
            st.error(f"Run failed: {e}")
            summary = None

    if summary:
        st.divider()
        k = st.columns(4)
        k[0].metric("Turns", summary["turns"])
        k[1].metric("Tokens", f"{summary['tokens']:,}")
        k[2].metric("Guard", "on" if summary["guard_on"] else "off")
        k[3].metric("Found the late delivery",
                    "yes" if summary.get("found_late") else "no")


        # Read the audit trail back out of Postgres for this run. A write we never
        # read is not an audit trail, so the app shows what actually landed.
        if summary.get("run_id"):
            try:
                rows = lakebase.run_summary(summary["run_id"])
                if rows:
                    with st.expander(f"Guard audit trail for this run "
                                     f"({len(rows)} decision(s) in Lakebase)"):
                        st.caption(f"run_id `{summary['run_id']}` — read back from "
                                   f"`tiffin.guard_decisions` in Lakebase Postgres.")
                        st.dataframe(
                            [{"turn": r["turn_index"], "tool": r["tool_name"],
                              "verdict": r["action"],
                              "p(fail)": r["failure_prob"],
                              "rewrote arg": "yes" if r["coercion_applied"] else "no",
                              "tool succeeded": ("yes" if r["tool_ok"] else
                                                 "no" if r["tool_ok"] is False else "-")}
                             for r in rows],
                            hide_index=True, use_container_width=True)
            except Exception as e:  # noqa: BLE001
                st.caption(f"Audit trail unavailable (Lakebase): {e}")

        st.session_state.setdefault("runs", {})[
            "on" if summary["guard_on"] else "off"] = summary

    runs = st.session_state.get("runs", {})
    if "on" in runs and "off" in runs:
        off, on = runs["off"], runs["on"]
        st.markdown('<div class="tf-turn">Guard off vs guard on</div>',
                    unsafe_allow_html=True)
        st.table({"": ["Turns", "Tokens", "Found the late delivery"],
                  "Guard off": [off["turns"], f"{off['tokens']:,}",
                                "yes" if off.get("found_late") else "no"],
                  "Guard on":  [on["turns"], f"{on['tokens']:,}",
                                "yes" if on.get("found_late") else "no"]})
        if off["tokens"] and on["tokens"]:
            saved = (f"The guarded run used {off['tokens'] - on['tokens']:,} fewer tokens "
                     f"and {off['turns'] - on['turns']} fewer turns.")
            if on.get("found_late") and not off.get("found_late"):
                st.caption("The late delivery is on page 2, behind the malformed token. "
                           "Unguarded the agent never gets there: it spends the turns "
                           "retrying and the dispatcher never learns the delivery is late. "
                           + saved)
            else:
                st.caption(saved)
