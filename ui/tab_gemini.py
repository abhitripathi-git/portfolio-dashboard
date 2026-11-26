"""Ask Gemini tab: large composer, free browser voice-to-text mic, and streaming Cloud Q&A."""

from __future__ import annotations

from typing import Any

import streamlit as st
import streamlit.components.v1 as components

from core.gemini_chat import (
    AVAILABLE_GEMINI_MODELS,
    ask_portfolio_gemini,
    load_env_api_key,
    save_env_api_key,
    stream_portfolio_gemini,
)
from ui.sidebar import SidebarSelection


def _render_mic_button() -> None:
    """Render a zero-cost browser Web Speech API mic button that streams text into the Ask box."""
    mic_html = """
<style>html,body{margin:0;padding:0;overflow:hidden;background:transparent;}</style>
<div style="display:flex;align-items:center;font-family:Inter,-apple-system,sans-serif;">
  <button id="micBtn" type="button" style="
    display:inline-flex;align-items:center;justify-content:center;gap:6px;box-sizing:border-box;
    width:100%;height:38px;padding:0 14px;border-radius:10px;cursor:pointer;
    border:1px solid rgba(148,163,184,0.32);background:rgba(148,163,184,0.09);
    color:inherit;font-size:13.5px;font-weight:550;transition:all .15s ease;">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
      <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
      <line x1="12" x2="12" y1="19" y2="22"/>
    </svg>
    <span id="micLabel">Speak</span>
  </button>
</div>
<script>
(function() {
  const btn = document.getElementById('micBtn');
  const lbl = document.getElementById('micLabel');
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition || (window.parent && (window.parent.SpeechRecognition || window.parent.webkitSpeechRecognition));
  if (!SR) {
    lbl.textContent = 'Mac: Fn×2';
    btn.title = 'Browser Speech API unavailable here — press Fn twice on Mac to dictate.';
    return;
  }
  let rec = null;
  let listening = false;
  let baseText = '';

  function findTextarea() {
    try {
      const doc = window.parent.document;
      return doc.querySelector('textarea[aria-label="Ask Gemini"]') || doc.querySelector('textarea');
    } catch (e) {
      return null;
    }
  }

  function setNativeValue(el, val) {
    const proto = window.parent ? window.parent.HTMLTextAreaElement.prototype : HTMLTextAreaElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }

  btn.addEventListener('click', function() {
    if (listening && rec) {
      rec.stop();
      return;
    }
    const ta = findTextarea();
    baseText = ta ? ta.value.trim() : '';
    rec = new SR();
    rec.lang = 'en-IN';
    rec.continuous = true;
    rec.interimResults = true;

    rec.onstart = function() {
      listening = true;
      lbl.textContent = 'Listening... (Stop)';
      btn.style.background = 'rgba(194, 65, 75, 0.14)';
      btn.style.borderColor = '#C2414B';
      btn.style.color = '#C2414B';
    };

    rec.onresult = function(ev) {
      let transcript = '';
      for (let i = 0; i < ev.results.length; i++) {
        transcript += ev.results[i][0].transcript;
      }
      const target = findTextarea();
      if (target) {
        const joined = baseText ? (baseText + ' ' + transcript.trim()) : transcript.trim();
        setNativeValue(target, joined);
      }
    };

    rec.onerror = function() {
      listening = false;
      lbl.textContent = 'Speak';
      btn.style.background = 'rgba(148,163,184,0.10)';
      btn.style.borderColor = 'rgba(148,163,184,0.35)';
      btn.style.color = 'inherit';
    };

    rec.onend = function() {
      listening = false;
      lbl.textContent = 'Speak';
      btn.style.background = 'rgba(148,163,184,0.10)';
      btn.style.borderColor = 'rgba(148,163,184,0.35)';
      btn.style.color = 'inherit';
    };

    rec.start();
  });
})();
</script>
"""
    components.html(mic_html, height=40)


def render_gemini_tab(
    selection: SidebarSelection,
    snapshot: dict[str, Any] | None = None,
) -> None:
    """Render an interactive Gemini chat dialog wired to the active SQLite database."""
    if "gemini_api_key" not in st.session_state:
        st.session_state["gemini_api_key"] = load_env_api_key()
    if "gemini_chat_history" not in st.session_state:
        st.session_state["gemini_chat_history"] = []
    if "gemini_model_choice" not in st.session_state:
        st.session_state["gemini_model_choice"] = AVAILABLE_GEMINI_MODELS[0]
    if st.session_state.pop("clear_gemini_draft", False):
        st.session_state["gemini_draft_prompt"] = ""

    current_key = st.session_state["gemini_api_key"]
    active_model = st.session_state["gemini_model_choice"]

    col_hdr, col_clear = st.columns([4.2, 1.0])
    with col_hdr:
        st.subheader("Ask Gemini About Your Portfolio")
        st.caption(
            f"Scope: **{selection.owner_label}** · Model: `{active_model}` · "
            "Streams raw portfolio & transaction data straight to Gemini Cloud."
        )
    with col_clear:
        st.write("")
        if st.button("Clear Chat", icon=":material/delete_sweep:", width="stretch", key="clear_gemini_chat"):
            st.session_state["gemini_chat_history"] = []
            st.rerun()

    with st.expander(
        "Settings & Configuration" + (" (Configured)" if current_key else " (API Key Required)"),
        expanded=not bool(current_key),
    ):
        model_choice = st.selectbox(
            "Gemini Model",
            options=AVAILABLE_GEMINI_MODELS,
            key="gemini_model_choice",
        )
        enable_live_tools = st.toggle(
            "Enable live benchmark backtest tools (slower multi-turn)",
            value=False,
            key="gemini_enable_tools",
            help="Off by default: streams raw portfolio data directly to Gemini Cloud in one fast shot.",
        )
        st.markdown(
            "Get an API key from [Google AI Studio](https://aistudio.google.com/apikey). "
            "Saving writes only to your local `.env` file."
        )
        key_input = st.text_input(
            "GEMINI_API_KEY",
            value=current_key,
            type="password",
            placeholder="AIza...",
            key="gemini_api_key_input",
        )
        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            if st.button("Use for This Session", width="stretch", key="use_key_session"):
                st.session_state["gemini_api_key"] = key_input.strip()
                st.success("API key active for this session.")
                st.rerun()
        with btn_col2:
            if st.button("Save to Local .env", type="primary", width="stretch", key="save_key_env"):
                if key_input.strip():
                    env_path = save_env_api_key(key_input.strip())
                    st.session_state["gemini_api_key"] = key_input.strip()
                    st.success(f"Saved key to {env_path.name}.")
                    st.rerun()
                else:
                    st.warning("Paste a valid API key first.")

    if not st.session_state["gemini_api_key"]:
        st.info("Open **Settings & Configuration** above and enter your `GEMINI_API_KEY` to start.")
        return

    # Render conversation history
    for msg in st.session_state["gemini_chat_history"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            tools_used = msg.get("tools_used")
            if tools_used:
                with st.expander(f"Tools queried ({len(tools_used)})", expanded=False):
                    for item in tools_used:
                        st.code(f"{item['tool']}({item['args']})", language="python")

    # Large, comfortable composer box + free live Mic button
    draft = st.text_area(
        "Ask Gemini",
        height=150,
        key="gemini_draft_prompt",
        label_visibility="collapsed",
        placeholder=(
            "Ask anything in plain English or click Speak to dictate...\n"
            "• Compare RC Capital vs Fintrekk Capital returns\n"
            "• Check mutual fund overlap across our active schemes\n"
            "• How much is our India vs Foreign allocation?"
        ),
    )
    act_mic, act_spacer, act_send = st.columns([1.2, 2.8, 1.4], vertical_alignment="center")
    with act_mic:
        _render_mic_button()
    with act_send:
        send_clicked = st.button(
            "Ask Gemini",
            icon=":material/send:",
            type="primary",
            width="stretch",
            key="gemini_send_btn",
        )

    user_prompt = draft.strip() if send_clicked and draft.strip() else ""
    if user_prompt:
        with st.chat_message("user"):
            st.markdown(user_prompt)

        prior_history = [
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state["gemini_chat_history"]
        ]

        with st.chat_message("assistant"):
            try:
                tools_used: list[dict[str, Any]] = []
                if enable_live_tools:
                    with st.spinner("Running benchmark/DB tools & querying Gemini..."):
                        result = ask_portfolio_gemini(
                            prompt=user_prompt,
                            history=prior_history,
                            api_key=st.session_state["gemini_api_key"],
                            model=model_choice,
                            active_owner=selection.owner_label,
                            db_path=selection.db_path,
                            precomputed_snapshot=snapshot,
                            enable_tools=True,
                        )
                    reply = result["reply"]
                    tools_used = result["tools_used"]
                    st.markdown(reply)
                    if tools_used:
                        with st.expander(f"Tools queried ({len(tools_used)})", expanded=False):
                            for item in tools_used:
                                st.code(f"{item['tool']}({item['args']})", language="python")
                else:
                    reply = str(
                        st.write_stream(
                            stream_portfolio_gemini(
                                prompt=user_prompt,
                                history=prior_history,
                                api_key=st.session_state["gemini_api_key"],
                                model=model_choice,
                                active_owner=selection.owner_label,
                                db_path=selection.db_path,
                                precomputed_snapshot=snapshot,
                            )
                        )
                    )
                st.session_state["gemini_chat_history"].append(
                    {"role": "user", "content": user_prompt}
                )
                st.session_state["gemini_chat_history"].append(
                    {"role": "assistant", "content": reply, "tools_used": tools_used}
                )
                st.session_state["clear_gemini_draft"] = True
                st.rerun()
            except Exception as exc:
                st.error(f"Gemini request failed: {exc}")
