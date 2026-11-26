"""Ask Gemini tab: interactive natural-language chat inside the Streamlit dashboard."""

from __future__ import annotations

from typing import Any

import streamlit as st

from core.gemini_chat import (
    AVAILABLE_GEMINI_MODELS,
    ask_portfolio_gemini,
    load_env_api_key,
    save_env_api_key,
)
from ui.sidebar import SidebarSelection


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
            "Pre-computes your portfolio locally on Mac for fast answers."
        )
    with col_clear:
        st.write("")
        if st.button("Clear Chat", width="stretch", key="clear_gemini_chat"):
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

    user_prompt = st.chat_input(
        "Ask anything about your holdings, RC Capital vs Fintrekk, India vs Foreign, benchmarks, or MF overlap...",
        key="gemini_chat_input",
    )

    if user_prompt:
        with st.chat_message("user"):
            st.markdown(user_prompt)

        prior_history = [
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state["gemini_chat_history"]
        ]

        with st.chat_message("assistant"):
            with st.spinner("Analyzing local portfolio data & querying Gemini..."):
                try:
                    result = ask_portfolio_gemini(
                        prompt=user_prompt,
                        history=prior_history,
                        api_key=st.session_state["gemini_api_key"],
                        model=model_choice,
                        active_owner=selection.owner_label,
                        db_path=selection.db_path,
                        precomputed_snapshot=snapshot,
                    )
                    reply = result["reply"]
                    tools_used = result["tools_used"]
                    st.markdown(reply)
                    if tools_used:
                        with st.expander(f"Tools queried ({len(tools_used)})", expanded=False):
                            for item in tools_used:
                                st.code(f"{item['tool']}({item['args']})", language="python")
                    st.session_state["gemini_chat_history"].append(
                        {"role": "user", "content": user_prompt}
                    )
                    st.session_state["gemini_chat_history"].append(
                        {"role": "assistant", "content": reply, "tools_used": tools_used}
                    )
                except Exception as exc:
                    st.error(f"Gemini request failed: {exc}")
