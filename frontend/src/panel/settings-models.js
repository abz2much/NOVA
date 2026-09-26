  _wireAiModels() {
    const root = this.shadowRoot;
    const markDirty = message => {
      const status = root.getElementById("aiApplyStatus");
      if (status) status.textContent = message || "Unsaved changes.";
    };
    root.querySelectorAll(".new-model-row").forEach(row => {
      const provSel = row.querySelector(".new-prov-select");
      const modelSel = row.querySelector(".new-model-select");
      const customInput = row.querySelector(".new-model-custom");
      const refreshBtn = row.querySelector(".new-model-refresh");
      if (!provSel || !modelSel) return;
      this._loadModelsFor(provSel.value, modelSel);
      provSel.addEventListener("change", async (e) => {
        const provider = e.target.value;
        modelSel.setAttribute("data-current", "");
        if (customInput) customInput.style.display = "none";
        await this._loadModelsFor(provider, modelSel);
        markDirty();
        this._updateRoleWarning(row);
      });
      modelSel.addEventListener("change", e => {
        if (e.target.value === "__custom__") {
          if (customInput) { customInput.style.display = ""; customInput.focus(); }
          this._updateRoleWarning(row);
          markDirty();
          return;
        }
        if (customInput) customInput.style.display = "none";
        modelSel.setAttribute("data-current", e.target.value);
        markDirty();
        this._updateRoleWarning(row);
      });
      if (customInput) {
        customInput.addEventListener("input", e => {
          const v = (e.target.value || "").trim();
          if (v) modelSel.setAttribute("data-current", v);
          markDirty();
          this._updateRoleWarning(row);
        });
      }
      if (refreshBtn) {
        refreshBtn.addEventListener("click", async () => {
          refreshBtn.disabled = true;
          try {
            await this._loadModelsFor(provSel.value, modelSel, { refresh: true });
          } finally {
            refreshBtn.disabled = false;
          }
        });
      }
    });

    root.querySelectorAll(".ai-endpoint").forEach(input => {
      input.addEventListener("input", () => {
        const provider = input.getAttribute("data-endpoint-provider");
        if (this._modelCatalog) delete this._modelCatalog[provider];
        const status = root.querySelector(`[data-endpoint-status="${provider}"]`);
        if (status) status.textContent = "Endpoint changed. Test it before choosing a profile.";
        markDirty();
      });
    });
    root.getElementById("aiOllamaNumCtx")?.addEventListener("input", () => markDirty());
    root.getElementById("aiHomeContextMaxEntities")?.addEventListener("input", () => markDirty());

    root.querySelectorAll(".ai-endpoint-test").forEach(button => {
      button.addEventListener("click", async () => {
        const provider = button.getAttribute("data-endpoint-provider");
        const input = root.querySelector(`.ai-endpoint[data-endpoint-provider="${provider}"]`);
        const status = root.querySelector(`[data-endpoint-status="${provider}"]`);
        button.disabled = true;
        if (status) status.textContent = "Testing…";
        try {
          const res = await this._hass.callWS({
            type: "nova/test_provider_endpoint", provider,
            endpoint: (input?.value || "").trim(),
          });
          if (!res || !res.ok) throw new Error((res && res.message) || "Endpoint test failed.");
          if (input) input.value = res.endpoint;
          this._modelCatalog = this._modelCatalog || {};
          this._modelCatalog[provider] = res.model_details ||
            res.models.map(id => ({ id, capabilities: [] }));
          root.querySelectorAll(`.new-model-row`).forEach(row => {
            const provSel = row.querySelector(".new-prov-select");
            if (provSel?.value === provider) {
              this._populateModelSelect(provider, row.querySelector(".new-model-select"), res);
            }
          });
          if (status) status.textContent = `Connected. ${res.models.length} model${res.models.length === 1 ? "" : "s"} found.`;
          markDirty("Endpoint tested. Changes are not saved yet.");
        } catch (err) {
          if (status) status.textContent = err?.message || "Could not test this endpoint.";
        } finally {
          button.disabled = false;
        }
      });
    });

    const selectProfileModel = (row, provider, requiredCapability) => {
      const catalog = (this._modelCatalog || {})[provider] || [];
      const match = catalog.find(item =>
        !requiredCapability || (item.capabilities || []).includes(requiredCapability));
      if (!match) return false;
      const provSel = row.querySelector(".new-prov-select");
      const modelSel = row.querySelector(".new-model-select");
      provSel.value = provider;
      modelSel.setAttribute("data-current", match.id);
      this._populateModelSelect(provider, modelSel, {
        models: catalog.map(item => item.id), model_details: catalog,
      });
      modelSel.value = match.id;
      this._updateRoleWarning(row);
      return true;
    };
    root.querySelectorAll("[data-ai-profile]").forEach(button => {
      button.addEventListener("click", () => {
        const profile = button.getAttribute("data-ai-profile");
        if (profile === "manual") {
          markDirty("Manual mode: choose each provider and model, then Apply.");
          return;
        }
        const textRoles = profile === "hybrid"
          ? ["classifier", "reasoning", "camrsn"]
          : ["llm", "classifier", "reasoning", "camrsn"];
        const missing = [];
        textRoles.forEach(role => {
          const row = root.querySelector(`.new-model-row[data-role="${role}"]`);
          const required = role === "llm" ? "tools" : "completion";
          if (!row || !selectProfileModel(row, "ollama", required)) missing.push(role);
        });
        if (profile === "local") {
          const visionRow = root.querySelector('.new-model-row[data-role="vision"]');
          if (visionRow) selectProfileModel(visionRow, "ollama", "vision");
        }
        markDirty(missing.length
          ? "Profile staged where compatible models were found. Test Ollama first to load capabilities for the remaining roles."
          : "Profile staged. Review the choices, then Apply.");
      });
    });

    root.getElementById("aiApply")?.addEventListener("click", async event => {
      const button = event.currentTarget;
      const status = root.getElementById("aiApplyStatus");
      const updates = {
        ollama_base_url: (root.querySelector('.ai-endpoint[data-endpoint-provider="ollama"]')?.value || "").trim(),
        custom_base_url: (root.querySelector('.ai-endpoint[data-endpoint-provider="custom"]')?.value || "").trim(),
        ollama_num_ctx: Number(root.getElementById("aiOllamaNumCtx")?.value || 8192),
        home_context_max_entities: Number(root.getElementById("aiHomeContextMaxEntities")?.value ?? 15),
      };
      root.querySelectorAll(".new-model-row").forEach(row => {
        const provSel = row.querySelector(".new-prov-select");
        const modelSel = row.querySelector(".new-model-select");
        const customInput = row.querySelector(".new-model-custom");
        updates[provSel.getAttribute("data-cfg-key")] = provSel.value;
        updates[modelSel.getAttribute("data-cfg-key")] =
          customInput && customInput.style.display !== "none"
            ? customInput.value.trim() : modelSel.value;
      });
      button.disabled = true;
      if (status) status.textContent = "Checking models and saving…";
      try {
        const res = await this._hass.callWS({ type: "nova/apply_ai_config", updates });
        if (!res || !res.ok) throw new Error((res && res.message) || "Could not apply AI settings.");
        if (status) status.textContent = res.message || "Saved. Nova is reloading.";
      } catch (err) {
        if (status) status.textContent = err?.message || "Could not apply AI settings.";
        button.disabled = false;
      }
    });

    this._wireCredentials();
  }

  // Provider availability (Phase 3, v7.108.0): labels each role's provider
  // <option> as "not configured" when unavailable, WITHOUT disabling it —
  // an administrator can still pick it and add the credential/endpoint
  // right after. Never a value, just a boolean-derived label; each
  // provider's own evidence only (see websocket.py's
  // _compute_provider_availability — this only renders what it returns).
  _markProviderAvailability(available) {
    if (!available) return;
    const root = this.shadowRoot;
    root.querySelectorAll(".new-prov-select").forEach(sel => {
      Array.from(sel.options).forEach(opt => {
        const base = opt.value;
        if (!(base in available)) return;
        opt.textContent = available[base] ? base : `${base} (not configured)`;
      });
    });
  }

  // Provider Credentials (Phase 2, v7.107.0): status is fetched once per
  // render (never cached across renders — a stale "configured" badge after
  // a clear elsewhere would be misleading) and a saved/cleared value is
  // never echoed back by the websocket commands, only `ok`.
  async _wireCredentials() {
    const root = this.shadowRoot;
    const rows = root.querySelectorAll("[data-cred-provider]");
    if (!rows.length || !this._hass) return;

    try {
      const res = await this._hass.callWS({ type: "nova/get_credential_status" });
      const status = (res && res.status) || {};
      root.querySelectorAll("[data-cred-status]").forEach(el => {
        const p = el.getAttribute("data-cred-status");
        const configured = !!status[p];
        el.textContent = configured ? "configured" : "not set";
        el.classList.toggle("cred-configured", configured);
      });
      this._markProviderAvailability(res && res.available);
    } catch (_) { /* leave the "…" placeholder on error */ }

    root.querySelectorAll(".cred-save").forEach(btn => {
      btn.addEventListener("click", async () => {
        const p = btn.getAttribute("data-cred-provider");
        const input = root.querySelector(`.cred-input[data-cred-provider="${p}"]`);
        const value = (input && input.value || "").trim();
        if (!value || !this._hass) return;
        try {
          const res = await this._hass.callWS({ type: "nova/set_credential", provider: p, value });
          if (res && res.ok) {
            input.value = "";
            const statusEl = root.querySelector(`[data-cred-status="${p}"]`);
            if (statusEl) { statusEl.textContent = "configured"; statusEl.classList.add("cred-configured"); }
            this._markProviderAvailability({ [p]: true });
          }
        } catch (err) { console.error(`Nova: failed to save credential for ${p}`, err); }
      });
    });
    root.querySelectorAll(".cred-clear").forEach(btn => {
      btn.addEventListener("click", async () => {
        const p = btn.getAttribute("data-cred-provider");
        if (!this._hass) return;
        if (!window.confirm(`Clear the stored ${p} credential? Any role still using it will stop working until a new key is set.`)) return;
        try {
          const res = await this._hass.callWS({ type: "nova/delete_credential", provider: p });
          if (res && res.ok) {
            const statusEl = root.querySelector(`[data-cred-status="${p}"]`);
            if (statusEl) { statusEl.textContent = "not set"; statusEl.classList.remove("cred-configured"); }
            // custom/ollama availability isn't credential-derived (endpoint
            // / always-on respectively) — only the four cloud providers'
            // availability tracks their own credential.
            if (["groq", "openai", "anthropic", "gemini"].includes(p)) {
              this._markProviderAvailability({ [p]: false });
            }
          }
        } catch (err) { console.error(`Nova: failed to clear credential for ${p}`, err); }
      });
    });
  }

