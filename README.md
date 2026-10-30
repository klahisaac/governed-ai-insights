# Governed AI Insights (v0.1 prototype)

A minimal working prototype of a "headless insight layer": it builds validated insights on top of governed metrics and delivers them to people and AI agents. It ships with a pharmaceutical domain pack (the launch of **Zelvora**, a fictional anti-KIT drug). **All data and documents are synthetic.**

The platform uses only the Python 3.11+ standard library. Only the Claude API writer needs the Anthropic SDK, which goes into the project's virtual environment.

```bash
uv venv .venv && uv pip install --python .venv/bin/python anthropic
source ~/.config/insight-prototype/anthropic.env   # ANTHROPIC_API_KEY, kept outside the project
```

```bash
python3 -m insight.domains.pharmaceutical.synthetic_warehouse   # build the synthetic warehouse (data/warehouse.db)
python3 -m insight.server                                       # http://localhost:8000
python3 -m unittest discover -s tests -v                        # platform invariant tests
python3 -m insight.evaluation_gate --narrator template          # evaluation gate (exit code 1 on failure)
.venv/bin/python -m insight.evaluation_gate --narrator claude-sonnet-5-5 -v   # Claude API (needs ANTHROPIC_API_KEY)
```

## Flow

```
Source ──▶ Semantic ──▶ Fact ─────────────┐
warehouse   metric        statistically   ├──▶ Narrator ──▶ Validator ──▶ Delivery
(SQLite,    definitions   computed facts  │    sentences +   numbers, dates,  API, page,
 Databricks) (TOML)                       │    evidence      wording, policy  audit log
                                          │    (facts, documents)
Documents ──▶ selection (role, region, snapshot date) ┘
                  ▲
               Policy (permissions, small cell suppression, forbidden wording, required caveats)
```

## Metrics

| Metric | Measure | Facts the analyzer produces |
|---|---|---|
| `new_to_brand_patients` weekly new patients | count, stacked switch/naive | week over week change, streak, switch share, region that moved most, provisional weeks (required) |
| `eight_week_persistence` 8-week persistence by start week | rate | pooled persistence over measurable start weeks, latest start week versus earlier, switch versus naive, start weeks not yet measurable (required) |

- Regions are the region level of a US pharmaceutical sales organization (Northeast, Southeast, Midwest, West). A territory covered by a single representative is much smaller; at this data volume almost every number at that level would fall under the small cell rule.
- Persistence is measured only for start weeks that have been followed for 84 days and whose claims have fully arrived. The most recent 14 weeks show no value.
- Rates follow the small cell rule too. If the numerator, the denominator or their difference (patients who did not persist) is under 11, that week's rate is hidden. So even the national view hides the first 6 weeks, and most of each regional view is hidden.
- To add a metric, add a TOML file (with SQLite and Databricks implementations) to `metrics/` and an analyzer, and register them in `pharmaceutical/__init__.py`.

- The numbers (chart and Facts) are complete without any AI. The summary sits on top and is shown only when it passes validation.
- The **Summary writer** on the page is a Narrator in the code. The page shows Rule-based template, Claude (LLM) and Unchecked LLM: failure simulation; the audit log and provenance record the code name and version (for example `template@0.4.0`).
- The same inputs (metric version, data snapshot, analyzer, narrator and policy versions, scope, document versions and hashes) always produce the same insight id.

## Writing summaries with documents

Markdown documents in `insight/domains/pharmaceutical/documents/` (with TOML front matter) give the summary its context.

- **Selection:** only documents that match the reader's audience, fall inside the reader's region scope and were published by the data snapshot date. So a Southeast document never reaches the Northeast regional sales director, and a document dated after the snapshot (`brand-plan-2027-draft`) is never used.
- **LLM input:** documents go in as data inside `<documents>`, and the prompt says to ignore any instruction inside them. `field-notes-midwest-october` carries a planted prompt injection.
- **Validation:** a sentence that uses a document must cite it in `document_ids`, and a document not given to this summary cannot be cited (`unknown_document`). A document's numbers ground only **sentences that cite no facts** (document context sentences). Numbers in a sentence that cites facts must come from those facts, so mixing the brand plan target of 100 with this week's 81 in one sentence is blocked. A document's dates may sit beside a fact. Causal wording is blocked whatever the evidence.
- **Known limit:** a sentence that cites only documents may use their numbers, so the "50 percent" in the injected document passes the number check. Today the overstatement check (words like "surged") blocks that sentence. Whether placing a document next to a change ("Separately, ... took effect on ...") implies a cause is for compliance to decide.

## Who builds what (Celldex team)

| Files | Contents | Owner |
|---|---|---|
| `insight/domains/pharmaceutical/metrics/*.toml` | new patient and 8-week persistence definitions, versions, approval dates, SQLite and Databricks implementations | **Director** (governed business definitions) |
| `insight/domains/pharmaceutical/analyzers.py` | per-metric analyzers: normal variation tests (Poisson, binomial), source of business share, region changes, persistence comparisons | **Senior Data Scientist** |
| `insight/domains/pharmaceutical/narrators.py` | choosing facts and documents and putting them into sentences; in practice an LLM call | **Director** (AI layer, Python) |
| `insight/domains/pharmaceutical/documents/` | brand plans, market access updates, medical guidance, field notes | each team (brand, market access, medical, sales) |
| `insight/domains/pharmaceutical/policy.toml` | suppression under 11, forbidden wording, roles | Director, Medical, Compliance (rule content) |
| `web/` | page and charts | **Design Engineer** (with the full-stack engineer) |
| `insight/core/*`, `insight/server.py`, `insight/evaluation_gate.py`, `tests/` | metric execution engine, caching, fact, claim and document contracts, validator, policy engine, authentication, audit, API, evaluation gate, tests | **Full-stack engineer** |
| `insight/domains/pharmaceutical/synthetic_warehouse.py`, `to_databricks.py` | synthetic claims data and the Databricks load | in practice data vendors and data engineering |

What the full-stack engineer builds is **the runtime that safely runs, connects and checks other people's definitions, analyses, prompts, documents and pages**. `insight/core` never imports `insight/domains`.

## Things to try on the page

1. **Brand lead:** open the national chart. The latest 2 weeks are provisional and hatched, and the summary ends with the approved caveat about those weeks, tagged **Platform** (a required fact). The platform writes that sentence, not the writer. Switch the Summary writer to **sonnet-5.5 (Claude)** to get a summary that cites documents; clicking a document chip opens it in the Documents card.
2. **Switch the Summary writer to Unchecked LLM: failure simulation:** it imitates invented numbers, causal wording, overstatement, wrong direction and following instructions inside a document (it also leaves out the caveats, but the platform adds the required ones anyway). The validator withholds the summary; the chart and Facts stay.
3. **Southeast regional sales director:** sees only their own region's numbers and documents. Weeks under 11 are hidden, and such facts cannot be used in the summary. Switching the region to NE returns 403 and is recorded in the audit log.
- **Cached or new, and regenerating:** the top of the Summary card says whether the summary came from the cache (Cached, with when it was written) or was written for this request (New, with time taken and cost). **Regenerate** skips the cache and writes a new one; for an LLM writer that costs money and is recorded as `summary_regenerated` in the audit log.
- **Cards per role:** `views` in `policy.toml` sets which cards each role sees. A regional sales director sees the chart, Summary, Definition and Documents; a brand lead also sees Facts; an administrator sees everything, including Policy, Provenance, Summary writer evaluation and the Audit log. The evaluation API follows the same list.
4. **Platform administrator:** the audit log shows who saw what, with which definition version and documents, and who was denied.
5. **Distribution levels:** a summary sentence takes the strictest level of the evidence it cites (data is `internal`, the brand plan is `confidential`). Copying `internal` text adds the level notice and the insight id; copying `confidential` text is blocked. Both are audited. Documents show their level as an "MLR grade" badge. A browser cannot stop screenshots, so real enforcement belongs to the company's DLP (for example Microsoft Purview sensitivity labels).
6. **Change the policy (administrator):** changing the minimum cell size in the Policy card redraws the chart, Facts and summary under the new rule. The policy version becomes something like `2026.10.1+minimum-cell-size-5`, so the insight id changes too, and the change is audited. `policy.toml` is not rewritten, so a restart returns to the approved value.

## Running on Databricks

```bash
export DATABRICKS_HOST=https://adb-....azuredatabricks.net
export DATABRICKS_WAREHOUSE_ID=...
export DATABRICKS_CATALOG=...
export DATABRICKS_SCHEMA=pharmaceutical
python3 -m insight.domains.pharmaceutical.to_databricks          # load the synthetic data (uses the az login token)
INSIGHT_DATABRICKS_TESTS=1 python3 -m unittest tests.test_databricks   # compare results with SQLite
INSIGHT_WAREHOUSE=databricks python3 -m insight.server 8001
```

Queries run in parallel, and results are cached by snapshot hash (the snapshot is checked every 60 seconds).

## Comparing summary writers: Claude and GPT on Microsoft Foundry

| Summary writer | Path | Authentication |
|---|---|---|
| sonnet-5.5 (Claude) | the Claude API through the Anthropic SDK (`claude-sonnet-5-5`, effort low, structured output, server-side fallback); `CLAUDE_MODEL` changes the model | `ANTHROPIC_API_KEY` (`~/.config/insight-prototype/anthropic.env`, outside the project) |
| gpt-5-mini (Microsoft Foundry) | v1 chat completions on a Foundry resource, strict JSON schema | Entra token (`Cognitive Services OpenAI User` role), no API key |

Both writers use **the same prompt and the same schema** and must pass the same validator. Only the model behind `StructuredLLM` changes. The Foundry writer appears in the list only when `FOUNDRY_ENDPOINT` is set.

```bash
FOUNDRY_ENDPOINT=https://<your-foundry-resource>.cognitiveservices.azure.com FOUNDRY_DEPLOYMENT=gpt-5-mini \
FOUNDRY_INPUT_PRICE_PER_MILLION=0.25 FOUNDRY_OUTPUT_PRICE_PER_MILLION=2.0 \
  python3 -m insight.evaluation_gate --narrator foundry-gpt-5-mini -v
```

Every evaluation gate run is appended to `data/evaluation_runs.jsonl`. Administrators can also run it from the page with **Re-evaluate** (Evaluate when there is no result yet; `evaluation:run` permission, audited as `evaluation_run`). It uses a separate engine, so it never overwrites summaries cached for readers. The **Summary writer evaluation** card (2 metrics × national and 4 regions = 10 cases) shows each writer's latest run: cases passed, cost, time, and the failing sentences with the rules they broke. The cases cover every region, so only roles that see every region (brand lead, administrator) see the card.

Recent comparison (10 cases, same schema): Claude Sonnet 5.5 10/10 ($0.084, 31 s), Claude Opus 5.5 10/10 ($0.18, 44 s), gpt-5-mini 9/10 ($0.016, 59 s). After the validator was tightened (writer versions Claude 0.3.0, Foundry 0.2.0): Sonnet 5.5 10/10 ($0.080); gpt-5-mini 8/10 on the first run, then 10/10 ($0.020) after the title quantity rule was fixed to accept the "8-week" form. The other failure in that first run mixed the brand plan target into a fact sentence, which the new rule caught as intended. With the same pass rate, Sonnet 5.5 is the default model. The Claude API's structured output does not accept `maxItems` or a type list with an enum (`["string", "null"]`), so the adapter drops `maxItems` and `direction` is defined with `anyOf`. Before the tightening, gpt-5-mini failed on "because" in a data caveat (the causal wording rule) and on a sentence that restated the metric definition without evidence. Prices are estimates from OpenAI's published pricing.

## Prototype stand-ins and their production counterparts

| Prototype | Production | Where it changes |
|---|---|---|
| `SqliteWarehouse` | `DatabricksWarehouse` (implemented) | `Warehouse` interface |
| `FileDocumentStore` | Databricks volume, search index (vector search) | `DocumentStore` interface |
| `PrototypeIdentityProvider` (tokens) | Entra ID (Azure) / Keycloak (on premises) OIDC | swap the `IdentityProvider` implementation |
| `ClaudeAPI`, `FoundryChat` (implemented) | the same, with workload identity instead of keys | `StructuredLLM` interface |
| standard library HTTP server | FastAPI | `server.py` |
| JSONL audit log | append-only Postgres table | `AuditLog` |
| console JSON logs, `/metrics`, OTLP traces (lab: Prometheus, Jaeger, Grafana) | Azure Monitor / Grafana | `telemetry.py`, `server.py` |

## Continuous integration (GitHub Actions)

`.github/workflows/ci.yml` runs on every pull request and on pushes to main.

| Job | What it proves | Runs on |
|---|---|---|
| `test` | unit tests on the SQLite stand-in: engine, policy, validator, writers | every run |
| `manifests` | every overlay renders and validates against Kubernetes and CRD schemas (kubeconform) | every run |
| `databricks` | every metric's Databricks SQL gives the SQLite answers, on the real warehouse | pull requests from this repository only |
| `kind` | the image builds and has no fixable critical or high CVEs (Trivy); deployed by `deploy/deploy.sh` (the script staging and production use) on a kind cluster with the pinned chart versions of kube-prometheus-stack and Jaeger, the app serves validated summaries, Prometheus scrapes it and loads its alert rules, Jaeger receives its traces, Grafana loads its dashboard | every run |

- **Databricks access without secrets:** the job signs in to Entra as the `governed-ai-insights-ci` app through a federated credential that trusts only `repo:klahisaac@254795426/governed-ai-insights@1438633210:pull_request`. GitHub puts the owner and repository ids in the token subject, so a deleted and recreated repository with the same name does not inherit the trust (recreating it means updating this subject). That service principal holds no Azure role, only `CAN_USE` on the SQL warehouse and `USE CATALOG`, `USE SCHEMA` and `SELECT` on the schema. The workspace settings are repository variables, not secrets. A fork's pull request gets no token, so the job is skipped there.
- **Same artifacts as the clusters:** manifests live in `deploy/base` with one overlay per environment (`lab`, `ci`, `staging`, `production`). Overlays change values only (image, environment name, identity, schema, routes); what every AKS environment shares is the `deploy/components/aks` component. Monitoring chart versions are pinned once in `deploy/platform/charts.env`, and `deploy/deploy.sh` installs them the same way everywhere except the lab.
- **What kind cannot show:** Workload Identity, the AKS network and storage, and the app reaching Databricks from inside a cluster. Staging on AKS checks those.
- `deploy/smoke-test.sh` is the same check for any cluster running an overlay.

## Staging and production on AKS

```
push to main ──▶ ci ──passes──▶ release: build once, push to ACR, deploy.sh staging <digest>,
                                         smoke test (app reads Databricks), tag staged-<commit>
gh workflow run promote -f commit=<sha> ──▶ approval ──▶ deploy.sh production <same digest>, smoke test
```

- **Two clusters** (`aks-governed-ai-insights-staging`, `-production`), one `Standard_D2s_v4` node each, each with its own kube-prometheus-stack and Jaeger from the pinned charts. Entra sign-in with Azure RBAC, no local accounts. One registry (`acrgovernedaiinsights`): staging pushes, production pulls. `deploy/azure/provision.sh` creates all of it and can be run again.
- **The app reads Databricks with no secret.** Each environment has a user-assigned managed identity, federated with that cluster's service account `insight/insight-platform` (Workload Identity). The app exchanges the projected service account token for an Entra token (`insight/core/azure_credentials.py`). In Databricks the identity holds `CAN_USE` on the SQL warehouse and `USE SCHEMA`, `SELECT` on its own schema only (`pharmaceutical_staging`, `pharmaceutical_production`). Pull request CI reads `pharmaceutical_demo`.
- **GitHub deploys with no secret either.** Each GitHub environment (`staging`, `production`) has a deploy identity federated with that environment only. It can sign in to its own cluster and nothing else; staging may push to the registry, production may only pull. The production environment needs an approval and accepts runs from main only.
- **The same image all the way:** release builds once and deploys by digest. Only a digest that passed the staging smoke test is tagged `staged-<commit>`, and promote deploys that digest, so production runs the bytes staging tested.
- **Public in production only:** production is https://insight.isaacon.dev: Cloudflare proxies the name (`deploy/cloudflare`, Terraform) to the AKS app routing NGINX (`deploy/overlays/production/ingress.yaml`), which redirects `/metrics` away. The prototype's sign-in tokens are in this repository, so production runs with `INSIGHT_PUBLIC=1`: everything stays on, and paid model calls (new summaries, regenerations, gate runs) share a quota of 1000 a day (`INSIGHT_MODEL_CALLS_PER_DAY`, reset at 00:00 UTC, counted in the data volume). Staging has no public route; the smoke test reaches both through `kubectl port-forward`. On AKS the Foundry writer signs in with the pod's identity; the Claude writer is off until its key comes from Key Vault.
- **Cost:** two nodes, two load balancers for outbound traffic (production's also carries the ingress IP) and the registry, roughly $150 to $200 a month while the clusters run. `az aks stop` on a cluster stops its node charges.

## Deploying to a Kubernetes lab cluster (Argo CD, Prometheus, Jaeger, Grafana)

`deploy/` puts the app on a lab cluster (single node `xklaus03x`, Cilium Gateway, kube-prometheus-stack, Jaeger, Argo CD, Gitea).

```
deploy/build-image.sh ──▶ node containerd      (no Docker, so Kaniko builds it inside the cluster)
deploy/overlays/lab ──publish-gitops.sh──▶ Gitea gitea_admin/insight-gitops ──▶ Argo CD (insight-platform app, auto sync)
                                                                          │
   insight namespace: Deployment, Service, PVC, HTTPRoute, ServiceMonitor, PrometheusRule, dashboard ConfigMap
   monitoring namespace: Jaeger data source for Grafana
```

```bash
deploy/build-image.sh 0.1.1                          # build the image and load it onto the node
# set newTag in deploy/overlays/lab/kustomization.yaml to 0.1.1, then
deploy/publish-gitops.sh "bump image to 0.1.1"      # commit to Gitea; Argo CD syncs
kubectl apply -f deploy/argocd/application.yaml     # once
```

- **App:** https://insight.k8s.internal (add the gateway IP 192.168.4.30 to hosts). The gateway blocks `/metrics`; it is scraped only inside the cluster.
- **Claude writer:** enabled only when the `insight-llm` Secret (`ANTHROPIC_API_KEY`) exists. It is created by hand and never committed. The Foundry writer needs an Entra token, so it is off in the cluster.
- **Data:** the synthetic warehouse is built into the image (fixed seed) and copied to the PVC (`insight-data`) on first start. The audit log and evaluation runs live on the same PVC. Files and an in-memory cache mean a single replica.
- **Prometheus (health):** `GET /metrics` is exported with the standard library (`insight/core/telemetry.py`): request count, latency and status codes, summary outcomes (accepted/withheld) and cache hits, writer latency, errors and LLM cost, validator rule violations, access denials, warehouse query latency. Alerts: down, 5xx above 5%, p95 above 10 s, writer errors, LLM cost above $2 per hour.
- **Jaeger (performance):** the image includes OpenTelemetry and sends traces when `OTEL_EXPORTER_OTLP_ENDPOINT` is set. A request shows as `GET /api/metrics/{name}` → `insight.view` → `warehouse.query` (parallel) → `narrator.narrate` (model, cost, tokens) → `anthropic.messages.create`. Log lines carry `trace_id`. Locally, without OpenTelemetry, it does nothing.
- **Grafana:** the `Insight Platform` dashboard (uid `insight-platform`): health (up, request rate, error rate, p95, restarts, alerts, version, CPU, memory), summaries and writers (pass rate, cache hit rate, cost, latency, rules violated), and a slow trace table (Jaeger, click through to the trace).
- **Limits:** the image goes straight onto the node with no registry, so this works only on a single node. In production the image is pushed to a registry (ACR or similar) and CI bumps the tag in the GitOps repository. Metrics have no spans inside the engine (analyzers, validator); the server instruments the warehouse and narrator by wrapping them, without changing the engine.

## Leaks closed after external review (Codex and Grok reviews)

- **Complement of the switch share:** `switch_share` checked only switch and total, so naive (total − switch) could be shown even at 10 or fewer. Naive is now a count field and is hidden with them.
- **Withheld drafts:** a draft that broke the rules goes only to the `draft:read` permission (administrators). Other roles see only the list of violations.
- **Copying withheld summaries:** copying a withheld summary or draft is blocked whatever its distribution level, and the attempt is audited.
- **Hidden weeks on the chart:** hidden and not yet measured weeks were drawn as bars of height 10 and 8, which could read as values. They are now full-height bands (dashed gray for hidden, hatched for not yet measured), so height means nothing.
- **Validator meaning checks:** the review found three gaps. They are now closed as follows.
  - Movement words in the text (rose, increased, fell, declined, surged and so on) are compared with the direction of the cited facts. Writing "rose" for a flat fact is a `direction_mismatch`. Comparatives such as higher and lower are not checked, because their meaning flips with what is compared.
  - A document's numbers ground only sentences that cite documents alone. A number in the metric title counts only as the quantity written there ("8 weeks", "8-week"); a bare 8 does not.
  - Required caveats are not left to the writer. The analyzer writes the approved wording (`Fact.statement`), and the engine adds that sentence to the end of every summary (`author: "platform"`, tagged Platform on the page). A caveat the writer wrote about the same fact is dropped. The validator's `missing_required` check stays as a backstop.
- **LLM error details:** they can carry provider messages or parts of the prompt, so readers see only the error type and the insight id, and the details go to the server log.

## Not done yet (known limits)

- **No complementary suppression across regions:** subtracting the other regions from the national value can reveal a hidden region's value.
- **Document selection is a metadata filter.** There is no relevance search for the question yet (embeddings, vector index). Document dates must be ISO dates to be validated.
- The LLM summary cache is in memory only, so after a restart the same insight id can get different sentences.
- A metric definition approval flow and a version comparison page are next. On AKS there is no public route with real sign-in, no Key Vault for the Claude key, and the platform charts are installed by the deploy rather than by Argo CD.
- The validator's wording checks are word lists, so they cannot catch every paraphrase. That is why the evaluation gate and scoring of production samples are needed as well.
- LLM output varies from run to run. The same prompt can pass the evaluation gate one time and fail the next, so a real gate should run each case several times and judge by pass rate.
