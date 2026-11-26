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


def _render_mic_and_send_controls() -> None:
    """Render Antigravity-style compact Mic + Arrow Send buttons with 1-click auto-stop & flush."""
    controls_html = """
<style>
  html,body{margin:0;padding:0;overflow:hidden;background:transparent;}
  .bar{display:flex;align-items:center;justify-content:flex-end;gap:8px;height:40px;font-family:Inter,-apple-system,sans-serif;}
  .mic-btn{
    display:inline-flex;align-items:center;justify-content:center;gap:5px;box-sizing:border-box;
    height:34px;padding:0 11px;border-radius:999px;cursor:pointer;
    border:1px solid rgba(148,163,184,0.35);background:rgba(148,163,184,0.12);
    color:#94A3B8;font-size:12.5px;font-weight:550;transition:all .15s ease;
  }
  .mic-btn:hover{background:rgba(148,163,184,0.20);}
  .mic-btn.listening{
    background:rgba(194,65,75,0.16);border-color:#C2414B;color:#FCA5A5;
  }
  .send-btn{
    display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box;
    width:34px;height:34px;border-radius:999px;cursor:pointer;
    border:none;background:#3B82F6;color:#FFFFFF;transition:all .15s ease;
  }
  .send-btn:hover{background:#2563EB;transform:translateY(-1px);}
  .send-btn:active{transform:translateY(0);}
</style>
<div class="bar">
  <button id="micBtn" class="mic-btn" type="button" title="Dictate (click again to stop, or just click Send)">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
      <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
      <line x1="12" x2="12" y1="19" y2="22"/>
    </svg>
    <span id="micLabel">Speak</span>
  </button>
  <button id="sendBtn" class="send-btn" type="button" title="Ask Gemini" aria-label="Ask Gemini">
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
      <path d="M12 19V5"/>
      <path d="m5 12 7-7 7 7"/>
    </svg>
  </button>
</div>
<script>
(function() {
  const micBtn = document.getElementById('micBtn');
  const micLbl = document.getElementById('micLabel');
  const sendBtn = document.getElementById('sendBtn');
  try {
    const parentColor = window.parent && window.parent.getComputedStyle(window.parent.document.body).color;
    if (parentColor) micBtn.style.color = parentColor;
  } catch (e) {}
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition || (window.parent && (window.parent.SpeechRecognition || window.parent.webkitSpeechRecognition));

  let rec = null;
  let listening = false;
  let baseText = '';
  let latestTranscript = '';

  function findTextarea() {
    try {
      const doc = window.parent.document;
      return doc.querySelector('textarea[aria-label="Ask Gemini"]') || doc.querySelector('textarea');
    } catch (e) {
      return null;
    }
  }

  function findHiddenSendButton() {
    try {
      const doc = window.parent.document;
      return doc.querySelector('.st-key-gemini_send_btn button') || doc.querySelector('button[aria-label="gemini_send_trigger"]');
    } catch (e) {
      return null;
    }
  }

  function syncTextarea(el, val, doBlur) {
    if (!el) return;
    const proto = window.parent ? window.parent.HTMLTextAreaElement.prototype : HTMLTextAreaElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, val);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    if (doBlur) el.blur();
  }

  function resetMicUI() {
    listening = false;
    micLbl.textContent = SR ? 'Speak' : 'Mac: Fn×2';
    micBtn.classList.remove('listening');
  }

  if (!SR) {
    micLbl.textContent = 'Mac: Fn×2';
    micBtn.title = 'Browser Speech API unavailable here — press Fn twice on Mac to dictate.';
  } else {
    micBtn.addEventListener('click', function() {
      if (listening && rec) {
        rec.stop();
        return;
      }
      const ta = findTextarea();
      baseText = ta ? ta.value.trim() : '';
      latestTranscript = '';
      rec = new SR();
      rec.lang = 'en-IN';
      rec.continuous = true;
      rec.interimResults = true;

      rec.onstart = function() {
        listening = true;
        micLbl.textContent = 'Listening';
        micBtn.classList.add('listening');
      };

      rec.onresult = function(ev) {
        let transcript = '';
        for (let i = 0; i < ev.results.length; i++) {
          transcript += ev.results[i][0].transcript;
        }
        latestTranscript = transcript.trim();
        const target = findTextarea();
        if (target) {
          const joined = baseText ? (baseText + ' ' + latestTranscript) : latestTranscript;
          syncTextarea(target, joined, false);
        }
      };

      rec.onerror = resetMicUI;
      rec.onend = resetMicUI;
      rec.start();
    });
  }

  sendBtn.addEventListener('click', function() {
    if (listening && rec) {
      try { rec.stop(); } catch (e) {}
      resetMicUI();
    }
    const ta = findTextarea();
    if (ta) {
      const currentVal = ta.value;
      syncTextarea(ta, currentVal, true);
    }
    setTimeout(function() {
      const realBtn = findHiddenSendButton();
      if (realBtn) realBtn.click();
    }, 75);
  });
})();
</script>
"""
    components.html(controls_html, height=42)


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

    # Render conversation + composer inside a fragment so Clear/Send reruns stay scoped to this tab.
    _render_gemini_chat_fragment(
        selection=selection,
        snapshot=snapshot,
        enable_live_tools=enable_live_tools,
    )


@st.fragment
def _render_gemini_chat_fragment(
    selection: SidebarSelection,
    snapshot: dict[str, Any] | None,
    enable_live_tools: bool,
) -> None:
    """Render conversation history and composer without triggering a full-app rerun on send."""
    if st.session_state.pop("clear_gemini_draft", False):
        st.session_state["gemini_draft_prompt"] = ""

    # Render conversation history
    for msg in st.session_state["gemini_chat_history"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            tools_used = msg.get("tools_used")
            if tools_used:
                with st.expander(f"Tools queried ({len(tools_used)})", expanded=False):
                    for item in tools_used:
                        st.code(f"{item['tool']}({item['args']})", language="python")

    # Large, comfortable composer box + Antigravity-style inline footer (model selector + mic + arrow send)
    draft = st.text_area(
        "Ask Gemini",
        height=140,
        key="gemini_draft_prompt",
        label_visibility="collapsed",
        placeholder=(
            "Ask anything in plain English or click Speak to dictate...\n"
            "• Compare returns by advisory source\n"
            "• Check mutual fund overlap across our active schemes\n"
            "• How much is our India vs Foreign allocation?"
        ),
    )
    act_model, act_spacer, act_ctrl = st.columns([1.8, 2.0, 1.4], vertical_alignment="center")
    with act_model:
        model_choice = st.selectbox(
            "Model",
            options=AVAILABLE_GEMINI_MODELS,
            key="gemini_model_choice",
            label_visibility="collapsed",
        )
    with act_ctrl:
        _render_mic_and_send_controls()

    st.markdown(
        "<style>.st-key-gemini_send_btn{position:fixed;left:-9999px;top:-9999px;width:1px;height:1px;overflow:hidden;opacity:0;}</style>",
        unsafe_allow_html=True,
    )
    send_clicked = st.button(
        "Send",
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
                st.rerun(scope="fragment")
            except Exception as exc:
                st.error(f"Gemini request failed: {exc}")
