# QAT 통합 설계·운영 문서

> 이 문서는 기존 `CORE_DESIGN_v0.1.md`, `ARCHITECTURE.md`, `PROJECT_STATE.md`, `DECISIONS.md`, `RISK_POLICY.md`, `COMPLIANCE_POLICY.md`, `SIMULATION_NOTES.md`의 핵심 내용을 하나로 통합한 현재 기준 문서다.
>
> 프로젝트: **QAT — Quant Automated Trading**  
> 대상: 한국주식 / 미국주식 / 암호화폐  
> 거래 시간축: 분봉 단타 ~ 단기 스윙  
> 목표: **법·시장질서·계좌생존성 제약 아래 지속 가능한 Net PnL을 연구·검증하는 자동매매 시스템**

---

## 1. 설계 철학

### 1.1 전략과 실행 권한 분리

전략 또는 AI는 거래 아이디어인 `TradeProposal`만 생성한다. Broker를 직접 호출할 권한은 없다.

```text
Strategy / AI
  → TradeProposal
  → Validation
  → Cost / Net Alpha
  → Risk
  → Compliance
  → Market Integrity
  → Order Coordinator
  → Execution Router
  → Paper / Shadow / Live Broker
  → Settlement
  → Portfolio Ledger
```

전략이 접근하는 거래 API는 `submit_trade_proposal()` 하나로 제한한다.

### 1.2 Fail-Closed

판정 어휘는 기본적으로 `PASS | BLOCK | UNKNOWN`이다. 안전 관련 정보가 부족하거나 모순되면 승인으로 추정하지 않는다. Live 단계에서 안전 관련 `UNKNOWN`은 기본적으로 거래 차단 대상이다.

### 1.3 Paper First

검증 순서는 다음을 기준으로 한다.

```text
Historical Backtest
→ Out-of-Sample
→ Walk-Forward
→ Paper Trading
→ Shadow Trading
→ Small Live
→ 검증 후 자본 확대
```

### 1.4 Net Profit 우선

평가 대상은 Gross 수익이 아니라 비용 후 수익이다.

```text
Net PnL
= Gross PnL
- Commission
- Tax
- Spread
- Slippage
- FX Cost
- Market Impact
- 기타 실행비용
```

Expected Net Alpha가 전략별 최소 기준에 미달하면 거래하지 않는다.

### 1.5 시장 분리

KR / US / CRYPTO는 공통 Core를 공유하지만 비용, 거래시간, 통화, 규정, 주문형식, Settlement, Broker 규칙은 시장별 Adapter로 분리하는 것을 장기 원칙으로 한다.

---

## 2. 현재 구현 아키텍처

### 2.1 단일 실행 경로

```text
StrategyGateway.submit_trade_proposal(proposal, reference_price)
  → ExecutionOrchestrator
      1. ProposalValidator
      2. NetAlphaGate
      3. RiskGate
      4. ComplianceGate
      5. MarketIntegrityEngine
      6. Order: CREATED → VALIDATED → APPROVED
      7. SettlementService.reserve()
      8. ExecutionRouter.route()
      9. GlobalOrderCoordinator.register()
     10. PaperBroker.submit_order()
  → AuditLog
```

체결은 별도 단계다.

```text
PaperBroker.simulate_fill()
  → Fill | None
  → SettlementService.apply_fill()
  → PortfolioLedger.apply_fill()
  → FillOutcome
```

`FillOutcome`은 `APPLIED`, `IGNORED_DUPLICATE`, `BLOCKED_TERMINAL`, `BLOCKED_CANCELLED` 등을 사용한다.

### 2.2 모듈 지도

| 책임 | 모듈 |
|---|---|
| 도메인 객체 / Order 상태기계 / Reservation | `qat.core.models` |
| PASS/BLOCK/UNKNOWN 공통 판정 | `qat.core.gate` |
| FX Provider / FX 오류 | `qat.core.fx` |
| TradeProposal 검증 | `qat.core.validator` |
| Orchestrator / StrategyGateway | `qat.core.orchestrator` |
| Portfolio Ledger | `qat.portfolio.ledger` |
| Reconciliation | `qat.portfolio.reconciliation` |
| Reservation / Fill 정산 | `qat.execution.settlement` |
| Cost / Net Alpha | `qat.cost.engine` |
| Global Risk | `qat.risk.gate` |
| Compliance | `qat.compliance.gate` |
| Market Integrity | `qat.integrity.engine` |
| Global Order Coordinator | `qat.execution.coordinator` |
| PAPER/SHADOW/LIVE Router | `qat.execution.router` |
| Level-1 Paper Broker | `qat.paper.broker` |
| 실행 불가 Live Stub | `qat.live.broker_stub` |
| Audit Log | `qat.audit.log` |
| 설정 로더 (연구용 엄격 로딩 포함) | `qat.config` |
| 전체 Wiring | `qat.app` |
| Historical Data (로딩·검증·합성 fixture) | `qat.data` |
| 기준전략 / Backtest / Metrics / Walk-Forward / Manifest / 저장 | `qat.research` |
| UI 서비스·HTTP 서버·정적 SPA | `qat.ui` |

---

## 3. 거래 도메인 계약

### 3.1 기본 타입

- Market: `KR | US | CRYPTO`
- Currency: 현재 `KRW | USD | USDT`, 필요 시 확장
- Side: `BUY | SELL`
- Order Type: 현재 `MARKET | LIMIT`
- Crypto Symbol: `BASE/QUOTE` 형식, 예: `BTC/KRW`
- Phase 0에서는 일반적인 Short Selling을 사용하지 않는다.

### 3.2 TradeProposal

전략이 생성할 수 있는 것은 주문이 아니라 제안이다.

주요 필드: `proposal_id`, `strategy_id`, `market`, `symbol`, `side`, `quantity`, `order_type`, `limit_price`, `created_at`, `signal_timestamp`, `expected_holding_period`, `expected_gross_return`, `confidence`, `reason_code`.

선택적으로 `target_position`, `stop_loss`, `take_profit`, `feature_snapshot_id`, `model_version`, `strategy_version` 등을 사용할 수 있다.

### 3.3 Order 상태기계

상태:

`CREATED, VALIDATED, APPROVED, SUBMITTED, PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELLED, REJECTED, EXPIRED, ERROR`

정상 흐름:

```text
CREATED → VALIDATED → APPROVED → SUBMITTED
→ PARTIALLY_FILLED → FILLED
```

취소 흐름:

```text
SUBMITTED → CANCEL_PENDING → CANCELLED
```

### 3.4 핵심 불변조건

- `FILLED` 주문은 추가 체결을 받을 수 없다.
- `CANCELLED` 주문은 체결되지 않는다. 실제 Broker에서 cancel/fill race가 발생하면 Reconciliation이 필요하다.
- 총 체결수량은 원 주문수량을 초과하지 않는다.
- 동일 `fill_id`는 두 번 회계 반영하지 않는다.
- Phase 0에서 SELL 수량은 보유수량을 초과하지 않는다.
- 잔액이 부족한 BUY는 승인하지 않는다.

---

## 4. Portfolio Ledger와 회계

### 4.1 중앙 원장

`PortfolioLedger`를 QAT 내부 회계의 기준으로 사용한다. 추후 Live에서는 Broker 계좌를 무조건 신뢰하지 않고 Reconciliation을 통해 비교한다.

관리 대상:

- 통화별 Cash / Reserved Cash
- 종목별 Position / Reserved Position
- Average Cost
- Realized / Unrealized PnL
- Commission / Tax / FX Cost
- Market Exposure
- Strategy Exposure 확장 지점

### 4.2 다중통화

현금은 통화별로 분리한다. Portfolio/Risk의 기준통화 평가는 명시적인 `FXRateProvider`를 사용한다.

- 기준통화는 설정에서 주입한다.
- 다른 통화의 환율을 1.0으로 추정하지 않는다.
- 환율이 없거나 0 이하이면 오류로 처리한다.
- Risk는 필요한 FX가 없으면 `UNKNOWN`으로 Fail-Closed한다.
- Batch #2.0: `realized_pnl_total()` / `equity()`의 1.0 대체 경로를 제거했다(비기준통화 환율 누락 → `MissingFXRateError`). 연구·UI는 `PortfolioLedger.valuation()`을 사용하며, 평가가격 누락(`missing_marks`)과 FX 누락(`missing_fx`)을 구분해 보고하고 하나라도 있으면 기준통화 합산값을 `None`으로 둔다.
- 한계: 기존 `equity_in()` / `exposure_in()`은 평가가격이 없을 때 평균원가로 대체한다. Risk 포트폴리오 한도가 하나도 설정되지 않으면 FX 검사를 하지 않는다. `RiskGate(mark_prices_fn=...)`를 주입한 경로(Backtest·UI)만 시점별 평가가격을 사용한다. 따라서 "모든 상황에서 FX 검증 완료"가 아니다.

### 4.3 평균원가와 실현손익

BUY 평균원가는 **매수측 취득비용**을 포함한다.

```text
BUY Cost Basis
= 매수 체결금액 + 매수 Commission + Tax + Exchange Fee + FX Cost
```

SELL 실현손익은 다음 원칙을 사용한다.

```text
Realized PnL
= Sell Proceeds
- 매도수량의 Cost Basis
- Sell-side Costs
```

매수 비용은 이미 평균원가에 들어 있으므로 매도 시 다시 차감하지 않는다.

Slippage는 `fill.price`에 반영된다. `slippage_estimate`는 분석용 기록이며 현금에서 두 번 차감하지 않는다.

---

## 5. Reservation과 Settlement

### 5.1 주문 전 예약

`APPROVED` 주문은 Broker 제출 전에 자원을 예약한다.

- BUY: 예상 현금 필요액 예약
- SELL: 매도 수량 예약
- 부족하면 `REJECTED`

예약금은 실제 체결 가능한 비용의 상한을 목표로 계산한다.

MARKET BUY의 기본 개념:

```text
slipped_notional
= quantity × reference_price × (1 + slippage_bps / 10000)

reservation
= slipped_notional
+ commission
+ exchange_fee
+ safety_buffer
```

LIMIT은 limit price를 기준으로 한다.

**OD-03 (CLOSED, Control Tower 결정)** — MARKET BUY 예약의 실행 버퍼(`execution_buffer_pct`, Backtest·UI 기본값)는 **2%를 유지**한다. 2% is a provisional policy value, not empirically validated: 실제 시장 데이터가 없어 바꿀 경험적 근거가 없다. 자동 튜닝하지 않고, 시장별 임의 값도 추가하지 않는다. 다음 봉 갭이 버퍼를 넘어 예약을 초과하면 기존 정책(5.3)대로 Breach를 기록하고 이후 주문을 차단한다. 실제 데이터로 갭 분포를 측정한 뒤 별도 결정한다.

### 5.2 부분체결 / 취소

- Partial Fill: 예약량을 비례 감소
- `FILLED`: 남은 예약 해제
- `CANCELLED / REJECTED / EXPIRED / ERROR`: 잔여 예약 전부 해제
- Reserved amount의 실질적 관리자는 `SettlementService`다.

### 5.3 Reservation Breach

실제 체결비용이 예상 예약금을 초과하더라도 실제 체결을 삭제하거나 비용을 숨기지 않는다.

```text
실제 체결비용 > 예약금
→ 실제 Fill과 비용을 그대로 Ledger 반영
→ reservation_breaches 기록
→ RiskGate가 신규 주문 BLOCK
→ Reconciliation 필요
```

이 정책은 V-04 실제 결함 재현 후 도입되었다.

Batch #2.0(C-1/C-2)에서 기록 조건을 문서 계약과 일치시켰다. 이전에는 BUY 후 가용현금이 음수일 때만 기록했으나, 이제 **각 BUY Fill의 실제비용(체결금액+비용)이 그 Fill이 소비한 예약 조각을 초과하면** 여유 현금 유무와 관계없이 기록한다(부분체결 포함). 실제 Fill과 비용은 그대로 원장에 남는다. Breach 해제는 `PortfolioLedger.resolve_reservation_breaches(recon, approver, note)`로만 가능하며 **깨끗한 Reconciliation 결과 + 이름 있는 승인자**가 필요하다. UI에는 해제 경로가 없다.

---

## 6. Cost Engine / Net Alpha Gate

시장별 Cost Adapter 확장을 전제로 한다.

평가 항목:

- Commission
- Tax
- Spread
- Slippage
- Market Impact
- FX Cost

현재 설정값은 연구용 placeholder이며 실제 시장의 확정 비용으로 간주하지 않는다.

### 6.1 OD-01 (CLOSED) — 노출 축소 주문의 Net Alpha 면제

Control Tower 결정: **보유 포지션의 절대 exposure를 감소시키는 주문은 Net Alpha Gate 적용 대상에서 제외한다.** 면제는 Net Alpha 임계값에만 적용된다.

- 판정 주체: `NetAlphaGate.reduces_exposure()`가 **Ledger(권위 있는 포지션)** 에서 판정한다. 제안 객체에는 exit/reduce 표시가 없고(`TradeProposal`에 필드 없음), UI는 `exit`·`reduce_only` 등 미허용 필드를 400으로 거부한다. `reason_code="exit"` 같은 문자열도 아무 효과가 없다.
- 조건: Long 보유 + SELL + `quantity ≤ 가용(미예약) 수량`. (부호 규칙을 일반화해 Short이 향후 지원되면 Short 보유 + BUY + `quantity ≤ |보유|`만 축소로 본다. 현재 Short는 지원하지 않는다.)
- 면제 아님: Long에 대한 BUY, 포지션 없는 SELL, 보유·가용 수량을 넘는 SELL(예: 100 보유 중 101 매도 → Net Alpha 적용, 이어서 Risk `insufficient_position`이 차단 — position flip·신규 Short·exposure 증가 불가), 이미 다른 SELL이 예약한 수량.
- 그대로 적용: 제안 검증, 실행 비용(수수료·세금·스프레드·슬리피지는 체결에서 그대로 부과), Risk, Compliance, Market Integrity, Settlement/Ledger, Kill Switch. 파이프라인 순서는 변경 없음.
- 감사: 면제 시 `net_alpha_decision`의 reasons에 `net_alpha_exempt:exposure_reducing`과 계산된 `expected_net_alpha`/`cost_fraction`이 남는다.
- 효과(합성 fixture, empirical 기대수익): 정책 전 청산 거절 KR ma_trend 189회·CRYPTO mean_reversion 619회 → 정책 후 0회. 남은 Net Alpha 거절은 모두 진입 제안이다.

### 6.2 위험 축소 청산과 Risk — CLOSED (Batch #2.2)

§7.1 참조. 분류 함수는 하나(`qat.core.exposure.reduces_exposure`)이며 Net Alpha 면제(OD-01)와 Risk 한도 완화가 같은 판정을 재사용한다.

---

## 7. Global Risk Engine

Risk는 Strategy보다 우선한다.

현재/장기 규칙:

- R-01 Max Order Notional
- R-02 Symbol Exposure
- R-03 Market Exposure
- R-04 Total Portfolio Exposure
- R-05 Daily Loss Limit
- R-06 Drawdown Limit
- R-07 Liquidity 부족 차단 *(향후)*
- R-08 비정상 Spread 차단 *(향후)*
- R-09 Volatility Shock 대응 *(향후)*
- R-10 Kill Switch

Kill Switch 후보 조건에는 Ledger 불일치, Broker 연결 이상, 데이터 손상, 불가능한 Risk 상태, 반복 주문 오류, 계좌 상태 불일치 등이 포함된다.

현재 Risk 한도 숫자는 실제 운용값으로 확정되지 않았다.

독립 감사(§12 of 검증 이력) 반영: R-06 Drawdown 한도가 설정되어 있는데 기준 equity(peak)가 관측된 적이 없으면 `UNKNOWN(drawdown_baseline_unknown)`으로 Fail-Closed한다(이전에는 조용히 PASS). Backtest·UI Paper 세션은 매 봉 `observe_equity`를 호출하고 날짜가 바뀌면 `start_new_day()`를 호출한다. 한도 위반 시 Risk가 위험 축소 SELL을 어떻게 다루는지는 §7.1에 정리했다(OPEN POLICY).

---

### 7.1 CLOSED — Risk-reducing exit hierarchy (Control Tower, Batch #2.2)

**판정 주체**: 서버/Core가 **권위 있는 Ledger**로 주문이 실제로 절대 exposure를 감소시키는지 판정한다(`qat.core.exposure.reduces_exposure`, Net Alpha 면제와 공용). 현재 Long-only 구조에서는 `Long 포지션 > 0 + SELL + 수량 ≤ 가용(미예약) 수량`일 때만 "노출 축소"다. `exit`·`reduce_only`·`risk_reducing` 같은 클라이언트 필드·`reason_code`는 읽지 않는다(TradeProposal에 필드 없음, UI는 미허용 필드 400). position flip·신규 Short·exposure 증가는 불가능하다.

| 규칙 | 노출 축소 SELL | 그 외(BUY / 비축소 SELL) |
|---|---|---|
| R-10 Kill Switch | **BLOCK — 예외 없음.** 최상위 Hard Stop(가장 먼저 검사) | BLOCK |
| R-05 Daily Loss | 한도 초과여도 **이 규칙 때문에 막지 않는다**(감사 reasons `exit_allowed:daily_loss_limit`) | BLOCK |
| R-06 Drawdown | 한도 초과여도 **이 규칙 때문에 막지 않는다**(`exit_allowed:max_drawdown`). 기준 equity 없음·FX 없음·비유한 값이면 **기존 UNKNOWN 유지** | BLOCK |
| 절대 노출 한도 `max_base_exposure` | 이미 초과여도 **막지 않는다**(150→120, 150→80 모두 가능, `exit_allowed:max_base_exposure`) | BLOCK |
| Reservation Breach | **조건부 허용**(아래) | BLOCK |
| R-01 주문금액/크기 한도 | **우회 불가** — 한도 초과 주문은 BLOCK. 자동 분할 없음(한도 이하의 여러 축소 주문으로 사람이/전략이 나눔) | BLOCK |
| Validation·비유한 값·수량/포지션 검증·Compliance·Market Integrity·실행 비용·Settlement·Ledger 무결성·Reconciliation 무결성 | **그대로 적용** | 그대로 |
| Net Alpha | 면제(OD-01, 임계값만) | 적용 |

**완화는 회계가 신뢰 가능할 때만** 준다: `Ledger.integrity_problems()`가 비어 있어야 한다(비유한 cash/PnL/포지션/Breach 값, 음수 포지션(Short), 음수 예약 현금, 예약 수량 > 보유 수량, 미해결 정산 불일치 기록). 회계가 신뢰 불가이면 위 완화는 적용되지 않고 이전 차단 동작이 유지된다(잘못된 accounting을 "축소 주문"이라는 이유로 PASS로 바꾸지 않는다).

**Reservation Breach — 재무 한도 위반과 회계 무결성 실패의 구분**: Breach(실제비용 > 예약, `kind=reservation_overrun`)는 *재무* 위반이다. 위험 증가 주문은 계속 BLOCK하지만 그것만으로 노출 축소 주문을 막지는 않는다. 다음을 **모두** 만족할 때만 허용한다: ① 실제 노출 축소 ② Breach가 plain overrun(종류가 다르면 `not_an_overrun` BLOCK) ③ Ledger 내부 일관성 OK ④ 최근 Reconciliation 기록에 불일치 없음(`reconcile()`이 결과를 Ledger에 기록하며, 깨끗한 실제 대조가 이후에 기록되면 회복) ⑤ 권위 있는 평가 가능(FX 없음 → UNKNOWN, 비유한 equity → BLOCK) ⑥ 다른 안전 Gate(Compliance/Integrity/주문 한도 등) BLOCK 없음. 하나라도 실패하면 BLOCK 또는 UNKNOWN(`accounting_untrusted` / `reconciliation_mismatch` / `fx_unavailable` / `reconciliation_not_performed`). `RiskGate(require_reconciliation=True)`이면 한 번도 대조하지 않은 상태의 Breach 청산은 UNKNOWN이다(Broker가 연결되는 모드는 반드시 True; 대조할 계좌가 없는 Paper 재생은 False). Breach 기록 자체는 청산으로 지워지지 않는다(해제는 깨끗한 대조 + 승인자, 5.3). 정산 불일치(`fill_order_mismatch`)는 `integrity_issues`로 남으며 `resolve_integrity_issues(recon, approver, note)`(깨끗한 대조 + 승인자)로만 해제된다.

**Kill Switch does not imply automatic liquidation.** Kill Switch 중에는 청산도 BLOCK이며, 포지션을 자동으로 정리하지 않는다. 긴급 강제 청산(Emergency flatten)·복구(recovery)가 필요하다면 **향후 별도 정책/설계**로 다루며 이번에 만들지 않았다(`flatten` 기능 없음).

구현 위치: `src/qat/core/exposure.py`(분류), `src/qat/risk/gate.py`(`relax`, `_breach_decision`), `src/qat/portfolio/ledger.py`(`integrity_problems`, `record_reconciliation`, `resolve_integrity_issues`), `src/qat/portfolio/reconciliation.py`(결과 기록), `src/qat/execution/settlement.py`(불일치 기록). 회귀: `tests/test_exit_policy_batch2_2.py`(38개).

## 8. Compliance / Market Integrity

### 8.1 Compliance

Compliance Engine은 법률판단 AI가 아니다. 명확하거나 잠재적으로 위험한 거래를 사전에 차단하는 소프트웨어 Guardrail이다.

검사 대상 예:

- 거래가능 종목 여부
- Trading Halt
- Broker/API 제한
- 계좌 제한
- 시장별 주문 제한
- stale/invalid proposal
- 검증되지 않은 종목/상태

Live 전에 최신 법령, KRX/미국시장/암호화폐 거래소 규정, Broker API 약관을 다시 확인해야 한다. 소프트웨어 `PASS`가 법적 적합성을 보장하지 않는다.

**OD-02 (CLOSED) — 거래 가능 종목(universe)**: 허용 목록이 없는 상태에서 외부 주문을 자동 허용하지 않는다(silent allow 금지, Fail-Closed).
- 코어 기본값: `ComplianceGate.DEFAULT_UNRESTRICTED_UNIVERSE = False`. 허용 목록도 명시적 opt-in(`unrestricted_universe=True`)도 없으면 `UNKNOWN(tradable_universe_not_configured)`. 허용 목록이 있으면 포함 종목만 진행하고(감사 reasons `universe:<label>`), 미포함 종목은 `UNKNOWN(symbol_not_verified)`이다(기존 T-018b 계약: PAPER/SHADOW에서 UNKNOWN, LIVE에서 BLOCK으로 승격). UNKNOWN도 Orchestrator가 진행시키지 않는다. 명시적 `unrestricted_universe=True`는 `universe:unrestricted_explicit`로 감사에 남는다(단위 테스트 스캐폴딩 전용: `tests/conftest.py`가 레거시 단위 테스트에 한해 명시적으로 opt-in하며, 프로덕션 진입점은 항상 universe를 명시한다).
- UI / Manual / Paper: `config/settings.yaml`의 `paper_universe` 목록만이 허용 근거다. 목록이 없거나 비어 있으면 모든 제안이 UNKNOWN이다. 저장소의 `SYNKR1`·`SYNUS1`·`SYNBTC/KRW`는 오프라인 검증용 fixture일 뿐 투자 universe 결정이 아니다.
- Research / Backtest: 임의의 allow-all을 쓰지 않는다. **검증을 통과한 입력 dataset의 선언된 market/symbol**이 해당 실행에만 적용되는 scoped universe다(감사 reasons `universe:research_run:<data_version>`). Manifest의 `universe` 필드(scope, market, symbols, source, data_version, `manual_trading_universe_effect: none`)로 추적한다. Research dataset은 `paper_universe`를 확장하지 못한다(별도 Compliance 인스턴스, 설정 불변).

### 8.2 Market Integrity

Compliance와 별도의 계층이다.

- MI-01 Duplicate Order
- MI-02 Opposing Orders
- MI-03 Self-Trade Risk
- MI-04 Excessive Order Frequency
- MI-05 Cancel/Replace Pattern *(향후)*
- MI-06 Liquidity Participation *(향후)*
- MI-07 Price Deviation
- MI-08 Abnormal Repetition

수익을 이유로 이 계층을 우회하지 않는다.

---

## 9. Global Order Coordinator / Execution Router

모든 Broker 주문은 Coordinator를 통과하는 것을 목표로 한다. Open/Pending Order, Portfolio Position, Strategy Position, Broker Position을 통합해 충돌을 찾는다.

전략은 Broker를 선택하지 않는다. 전략은 예를 들어 `BUY AAPL`을 제안하고, 향후 Coordinator/Venue Router가 실행처를 결정한다.

Router Mode:

- `PAPER`
- `SHADOW`
- `LIVE`

현재 `LIVE`는 실행 불가능하다. `ExecutionRouter`가 예외를 발생시키며 `LiveBrokerStub`도 실행을 거부한다.

---

## 10. Paper Broker — 현재 현실성 한계

현재 구현은 **Level 1 Simple**이다. 실제 체결을 재현한다고 주장하지 않는다.

### 10.1 체결 모델

- 호출자가 제공한 `reference_price`를 사용한다.
- Order Book / Depth / Queue Position / Market Impact Curve가 없다.
- `simulate_fill()` 호출자가 체결 및 부분체결을 유도한다.
- Broker가 시간 흐름을 스스로 진행하지 않는다.

### 10.2 LIMIT 주문

- BUY LIMIT: `reference_price <= limit_price`일 때만 체결 가능
- SELL LIMIT: `reference_price >= limit_price`일 때만 체결 가능
- 조건 미충족 시 `None` 반환, 주문은 OPEN 상태 유지
- Slippage가 있어도 LIMIT보다 불리한 가격으로 체결되지 않도록 clamp
- 더 유리한 가격 체결은 허용

### 10.3 비용

- Commission: notional × commission rate
- Exchange Fee: notional × exchange fee rate
- Tax: SELL notional × sell tax rate
- Paper Broker의 fill-time `fx_cost`는 현재 0 (설정의 `fx_cost_bps`는 Net Alpha 추정에만 사용 — 보수적 과대추정)
- Batch #2.0: 설정 기반 Stack(`build_paper_stack_from_settings`, Backtest, UI)은 시장별 CostModel을 Paper Broker에 연결한다. MARKET 주문 가격은 `half_spread_bps + slippage_bps`만큼 불리하게 이동하고(스프레드 절반을 건너고 미끄러짐), 수수료·매도세는 해당 시장 값으로 부과된다. 예약금도 같은 비율로 계산한다. CostModel이 없는 시장 주문은 거부한다(무비용 대체 금지).

### 10.4 Slippage

현재는 고정 BPS 방식이다.

- BUY: `ref × (1 + bps/10000)`
- SELL: `ref × (1 - bps/10000)`

변동성, 유동성, Spread, 주문크기 기반 모델은 아직 없다.

### 10.5 Latency / Cancel Race

- Latency 모델 없음
- Paper의 `cancel_order()`는 즉시 성공
- 실제 Broker의 cancel/fill race는 아직 Reconciliation 정책으로 해결하지 않음

### 10.6 결정론

고정된 `now_fn`과 Slippage를 사용하면 동일 입력에서 동일 Fill이 생성되도록 테스트되어 있다.

---

## 11. Reconciliation / Audit / Reproducibility

### 11.1 Reconciliation

Live 단계에서는 내부 Ledger와 Broker 계좌의 Cash, Position, Open Order, Fill 등을 주기적으로 비교해야 한다. 불일치는 BLOCK 또는 Kill Switch 후보다.

Batch #2.0: `reconcile()`은 내부·Broker 키의 **합집합**을 비교한다. 내부에만 있는 현금/포지션은 `missing_in_broker` 불일치, 빈 snapshot은 `snapshot_empty`, `complete=False`는 `snapshot_incomplete`로 항상 `ok=False`다. NaN/Infinity 잔고·수량 또는 비정상 `tol`은 불일치(`non_finite`) / ValueError로 처리한다(감사 D1). 비교 기능과 복구 승인(Breach 해제·Kill Switch 해제)은 분리되어 있다. Snapshot 시각(stale) 판별은 아직 없다(UNKNOWN). 현재 Paper 재생에는 대조할 실제 Broker 계좌가 없으므로 UI는 대조 상태를 `UNKNOWN`으로 표시한다.

### 11.2 Audit Log

중요 판단을 기록한다.

- Market Data Snapshot ID
- TradeProposal
- Cost Estimate
- Risk / Compliance / Integrity 판정
- 최종 Order
- Broker Response
- Fill
- PnL
- 향후 Strategy Health State

현재 구현: 메모리 `AuditLog`(주입 가능한 `now_fn`)에 제안·Gate 판정·예약·제출·`fill_settled`(Fill 결과·비용·통화별 실현손익)·`reservation_breach`·`reservation_released`가 기록된다. Backtest는 실행별 `audit.jsonl`로 저장한다. UI Paper 세션의 감사 기록은 메모리에만 있다(영속 감사 저장소는 미구현).

### 11.3 실험 재현성

Batch #2에서 구현했다(§15.4). 연구 실행에는 다음을 저장한다.

- `strategy_version`
- `model_version`
- `config_version`
- `data_version`
- `code_commit`
- `random_seed`
- 비용모델 버전

---

## 12. Strategy / ML 장기 정책

### 12.1 Strategy Virtual Ledger

실제 계좌의 Physical Portfolio Ledger와 전략별 Virtual Ledger를 분리할 계획이다. 예를 들어 계좌 전체는 Long이지만 특정 전략은 해당 종목에 대해 상대적으로 Short 노출을 가질 수 있다.

### 12.2 Strategy Health

장기적으로 손실 원인을 단순히 "모델 실패"로 취급하지 않는다.

Health Domain:

- Data
- Execution
- Alpha
- Model
- Regime
- Risk

상태: `GREEN / YELLOW / ORANGE / RED / BLACK`.

Critical Data Integrity Failure 등 Hard Failure는 평균점수와 무관하게 `BLACK`이 될 수 있다.

```text
Health Alert
→ Root Cause Analysis
→ Drift Check
→ Offline Retraining
→ Challenger
→ OOS / Walk-Forward
→ Paper
→ Champion vs Challenger
→ 사용자 승인
→ Deployment
```

손실만을 이유로 자동 재학습/자동 모델 교체하지 않는다.

### 12.3 연구 전략

초기에는 복잡한 ML/RL보다 단순하고 설명 가능한 Benchmark Strategy를 사용해 데이터·Backtest·비용·회계 파이프라인부터 검증한다. 복잡한 모델은 단순 기준전략 대비 비용 후 개선이 입증될 때 도입한다.

---

## 13. 현재 구현 상태

### 13.1 구현 완료

- Market / Currency / Side / OrderType / TradeProposal / Order / Fill / Position
- 11-state Order State Machine
- Multi-Currency Portfolio Ledger
- Average Cost / Realized PnL / Fee-Tax Accounting
- FX Provider + Missing FX Fail-Closed
- Reservation / Settlement
- Cost Engine / Net Alpha Gate
- Risk Gate 기본 규칙
- Compliance Gate
- Market Integrity 기본 규칙
- Global Order Coordinator
- PAPER/SHADOW/LIVE Router
- Level-1 Paper Broker
- Audit Log
- Live Broker Stub
- Config 기반 Wiring
- V-04 Reservation Breach 방어
- Batch #2.0 결함 수정: 설정 비용의 Broker/예약 연결, mode·markets 반영, FX 1.0 대체 제거, 예약 초과 판정, Reconciliation 합집합·빈 snapshot, Fill-주문 일치·과체결 방지·정산 원자성, 비유한 수치 차단, 결정론적 ID/시계 주입, 정산 감사, 제출 직렬화(lock)
- Historical Data Engine (로컬 CSV / Parquet은 pyarrow 설치 시, 검증 보고서, data_version·SHA-256, 합성 fixture)
- Backtest Engine (Core 파이프라인 경유, t 종가 신호 → t+1 시가 체결, 시뮬레이션 시계 주입)
- 기준전략 3종, Metrics, Cost Stress
- Walk-Forward / OOS 재사용 표시 / Lockbox / Manifest / 결과 저장·재조회
- 로컬 UI (Paper 데이터셋 재생, 안전 잠금 영속화)
- Batch #2.1 정책: OD-01 노출 축소 주문 Net Alpha 면제, OD-02 universe fail-closed(research run 범위 분리), OD-03 예약 버퍼 2% 유지(잠정값)
- Batch #2.2 정책: Risk-reducing exit hierarchy 종결(§7.1) — 공용 노출 축소 판정, Kill Switch 예외 없음, Daily Loss·Drawdown·절대 노출 한도 완화, Reservation Breach 조건부 허용, Ledger 무결성·Reconciliation 기록
- Protocol v2 개발 실행·후보 동결·one-shot holdout 평가기(§15.10): Official Development Run #1 결과(development evidence), 개발 선정 후보, 2026 평가기(미실행)
- Batch #3E Protocol v2 사전등록·freeze(§15.9): 연속 구간 research view, 비용 시나리오 worst-case 선택 규칙, 후보 판정 규칙, 시간적 holdout 2026, 사양 해시 동결
- Batch #3D BTCUSDT 4h 기반(§15.8): 4h 검증 데이터 foundation(엄격 완전성 FAIL 기록), 비용 v2 계약, per-trial series 저장, 정렬 벤치마크 v2, 2026 holdout 가드, Protocol v2 사전 판정
- Batch #3C 연구 타당성 기반(§15.7): trial registry·결정론적 trial 식별, 비용 증거 분류(VERIFIED/EVIDENCE_SUPPORTED/PLACEHOLDER/UNKNOWN), 다중검정 방법 평가, 벤치마크 audit, Protocol v2 판정
- Batch #3B Walk-Forward 연구 프로토콜(§15.6): 동결 프로토콜 `wf-protocol-1`, 시장별 fold, 후보 판정 규칙, Lockbox eligibility 판정(Lockbox는 열지 않음)
- Batch #3A 실제 시장 데이터 기반(§15.5): 불변 원본 + provenance, 결정론적 정규화·identity, 검증 V01~V18, 거래소 캘린더 snapshot, 보정(adjustment) 의미 판정, 교차검증, 연구 투입 admission(fail-closed), KR 공식 소스(data.go.kr) 검증

- **QAT v1 최종 완료(§19)**: Stale snapshot 정책, 영속 해시체인 감사 로그, Strategy Health(운영 상태), R-07/R-08/R-09·MI-05/MI-06, 운영자 전용 긴급 청산과 복구 상태 기계·재시작 평가, Broker 어댑터 경계(계약+Fake, 실패 의미론), Paper 졸업/Live 준비도 평가기, 운영 상태 요약, 시작 점검, 설정(ops.yaml) 검증, 원자적 상태 저장, 운영·감사·복구 UI

### 13.2 의도적으로 미구현/연기

- Strategy Virtual Ledger
- Paper Broker L2/L3
- 현실적 Latency / Spread / Liquidity / Market Impact (R-08은 규칙만 구현, 호가 데이터가 없어 설정 시 UNKNOWN — §19.5)
- Live cancel/fill-race reconciliation
- **실제** Broker Adapter (인터페이스·Fake 어댑터·경계는 구현, 실제 SDK·자격 증명은 없음 — §19.7)
- 실시간 Market Recorder
- ML/RL Strategy
- News/Context Intelligence
- 거래소 휴장일 캘린더 / 실시간 Paper 시세
- Shadow 시스템 (Router의 SHADOW는 PAPER와 같은 Broker 선택 경로일 뿐)

(§19에서 구현 완료: Strategy Health, MI-05, MI-06, R-07/R-09 및 R-08 규칙, 영속 감사 저장소, Reconciliation Snapshot 시각(stale) 판별)

### 13.3 아직 UNKNOWN

데이터와 실험 없이 임의 확정하지 않는다.

- 첫 Broker/Exchange Adapter
- 첫 Data Vendor (KR 일봉의 **공식 검증 소스**는 data.go.kr로 확인됨 — §15.5. 해외·암호화폐 및 실거래용 vendor는 UNKNOWN)
- 실제 Risk Limit 수치
- Capital Allocation 수치
- Paper 졸업 기준 (소유자 정의는 UNKNOWN. §19.8의 평가기는 **잠정 공학 수치**로 측정 가능하게만 만든 것이며 Alpha·수익성과 무관)
- Initial Live Capital
- 시장별 Universe
- ML Model 종류
- Training Window / Retraining Frequency
- 실제 지속 가능한 Net Alpha

---

## 14. 확정 설계 결정 요약

기존 D-001~D-018을 중복 없이 통합한 결정 기록이다.

| ID | 결정 |
|---|---|
| D-001 | Strategy/AI는 Live Broker를 직접 호출하지 않는다. |
| D-002 | Paper가 Live보다 선행한다. |
| D-003 | 대규모 Repository 구현은 AI에게 Batch 단위로 맡기되 검증은 별도로 수행한다. |
| D-004 | 구조는 KR/US/Crypto를 지원하되 E2E 검증은 한 시장부터 시작할 수 있다. |
| D-005 | 설계 변경은 문서와 검증 이력에 기록한다. 현재는 이 통합문서가 설계 기준이다. |
| D-006 | KR/US는 일반 ticker, Crypto는 `BASE/QUOTE` 형식을 사용한다. |
| D-007 | Currency는 KRW/USD/USDT에서 시작해 필요 시 확장한다. |
| D-008 | TradeProposal 필수계약은 Pipeline 진입의 Validator가 Fail-Closed로 검사한다. |
| D-009 | Cost/Risk/Net-Alpha 수치는 설정파일에 두며 현재 값은 placeholder다. |
| D-010 | Paper Broker는 Ledger를 직접 변경하지 않고 Fill을 반환한다. |
| D-011 | 손익·비용은 통화별로 기록하고 명시적 FX가 있을 때만 기준통화로 합산한다. |
| D-012 | 초기 Reservation primitive는 이후 FIX-02에서 실제 Settlement 경로에 연결되었다. |
| D-013 | Live Router와 Live Broker는 Phase 0에서 실행 불가능하다. |
| D-014 | FX는 명시적 Provider를 사용하고 누락/비정상 환율은 추정하지 않는다. |
| D-015 | APPROVED 주문은 제출 전에 현금/수량을 예약하고 SettlementService가 예약을 관리한다. |
| D-015a | Reservation 초과 실제체결은 숨기지 않고 기록한 뒤 신규 주문을 차단한다. |
| D-016 | BUY 비용은 평균원가에 포함하고 SELL 시 매수비용을 이중 차감하지 않는다. Slippage도 이중 과금하지 않는다. |
| D-017 | Terminal Order와 Duplicate Fill은 Ledger를 변경하지 못한다. |
| D-018 | LIMIT 주문은 가격조건을 만족할 때만 체결하며 limit보다 불리한 가격 체결을 금지한다. |
| D-019 | 설정 기반 Paper Broker는 시장별 수수료·매도세를 부과하고 MARKET 가격에 ½스프레드+슬리피지를 반영한다. `fx_cost_bps`는 Gate 추정 전용이다. |
| D-020 | Reservation Breach = BUY Fill 실제비용 > 소비한 예약 조각(여유 현금 무관). 해제는 깨끗한 대조 + 이름 있는 승인자만. |
| D-021 | Reconciliation은 합집합 비교, 빈/불완전 snapshot은 불일치. 비교와 복구 승인은 분리한다. |
| D-022 | Settlement는 Fill-주문 일치(주문ID·시장·종목·방향·통화)와 과체결을 검사하고, 검증 → 원장 → 예약 소비 순서로 원자적으로 처리한다. |
| D-023 | NaN/Infinity 수량·가격·비용·환율·기대수익은 도메인 생성 또는 Gate에서 거부한다. |
| D-024 | 연구 실행은 엄격한 설정 로딩을 사용한다. 무비용 실행은 명시적 `zero_cost_fixture`로만 가능하며 결과에 표시된다. |
| D-025 | Backtest 시간축: bar t 종가 후 신호, 가장 빠른 체결은 t+1 시가, 봉 내부 고가/저가 순서를 가정하지 않는다. 종료 시 청산하지 않고 종가 평가한다. |
| D-026 | Gross = 체결가 기준(슬리피지·스프레드 포함) 명시적 수수료·세금 차감 전, Net = 원장 평가액 변화. 계산 불가 지표는 None + 사유. |
| D-027 | Walk-Forward는 Train 구간 성과로만 파라미터를 선택하고 OOS를 1회 평가한다. 같은 데이터·전략의 재실행은 비독립 OOS로 표시하고, Lockbox는 1회만 사용한다. |
| D-028 | run_id는 실행 식별자(매번 다름), 실행 내부 ID·시각은 결정론적이다. 경제적 재현성은 economic_fingerprint로 확인한다. Git 정보가 없으면 unavailable + 소스 트리 해시. |
| D-029 | UI는 표준 라이브러리 HTTP 서버 + 정적 SPA(의존성 추가 없음). Paper는 데이터셋 재생이며 가격·승인은 서버가 결정한다. Kill Switch/Breach는 영속 잠금이며 UI에서 해제할 수 없다. |
| D-030 | Drawdown 한도가 설정되었는데 equity 기준이 없으면 UNKNOWN (조용한 PASS 금지). Paper 세션도 Risk 상태 훅(equity·일 시작)을 구동한다. |
| D-031 | Reconciliation은 비유한 값을 불일치로 처리한다. |
| D-032 | Lockbox는 같은 data_version의 겹치는 구간 재평가를 재사용으로 본다(one-shot). |
| D-033 | UI는 루프백 Host/Origin만 허용하고 숫자 입력(bool·NaN·Inf)을 서버에서 거부한다. |
| D-034 | (OD-01) 노출 축소 주문(Ledger 기준, 가용 수량 이내의 반대 방향 주문)은 Net Alpha 임계값만 면제한다. 클라이언트 자기 신고 불가, flip·신규 포지션·exposure 증가 불가, 다른 모든 Gate·비용·Settlement·Kill Switch는 그대로. |
| D-035 | (OD-02) 허용 목록 없이는 어떤 종목도 자동 허용하지 않는다. Manual/Paper는 `paper_universe`, Research는 검증된 dataset의 declared symbol(해당 run에만, Manifest 기록). 둘은 분리된다. |
| D-036 | (OD-03) 예약 버퍼 기본 2%는 잠정 정책값이며 경험적으로 검증되지 않았다. 자동 튜닝·시장별 임의 값 금지. |
| D-037 | (Exit hierarchy) 노출 축소 여부는 Ledger 기반 공용 함수 하나로 판정한다. Kill Switch는 예외 없이 BLOCK(자동 청산 아님, 긴급 청산은 향후 별도 정책). Daily Loss·Drawdown·절대 노출 한도는 노출 축소 SELL을 막지 않되 UNKNOWN 의미는 유지하고, 위험 증가 주문은 계속 BLOCK. |
| D-038 | (Exit hierarchy) Reservation Breach는 plain overrun + 회계 일관성 + 대조 불일치 없음 + 권위 있는 평가 가능일 때만 축소 주문을 허용한다. 재무 한도 위반과 회계 무결성 실패를 구분한다. |
| D-039 | (Exit hierarchy) 주문금액/크기 한도는 축소 주문도 우회 불가, 자동 분할 없음. Compliance·Integrity·Settlement·실행 비용은 그대로. 한도 완화는 회계 무결성 문제가 없을 때만. |

---

## 15. Research Pipeline (Batch #2)

### 15.1 데이터 계약

- 입력: 로컬 CSV(필수 컬럼 `timestamp,open,high,low,close,volume`) + `<file>.meta.json`(market, symbol, timeframe, timezone, timestamp_label, source, synthetic). Parquet은 `pyarrow`가 있을 때만(없으면 `DataBlocked`).
- `Bar.ts` = 봉 시가시각(UTC). `close_time = ts + timeframe`. 종가시각 라벨 파일은 시가시각으로 정규화한다.
- 검증은 수정하지 않는다: 보간·행 삭제 없음. ERROR(파싱 불가·결측·비유한·가격≤0·OHLC 불일치·음수 거래량·중복·역순) → `FAIL` → 연구/재생 BLOCK. WARN(0 거래량, 미설명 평일 공백, 세션 경계 누락, 분류 불가 공백). INFO(주말·야간 세션 공백).
- 세션 모델: KR 09:00–15:30(+09:00), US 09:30–16:00(America/New_York, tz DB 필요), CRYPTO 24/7. 거래소 휴장일은 모델링하지 않는다(UNKNOWN). 일봉은 파일에 선언된 timezone의 날짜로 판단한다.
- `data_version = <market>-<symbol>-<tf>-<sha256[:12]>-<meta_hash[:6]>`.

### 15.2 Backtest 시간축과 체결

1. open(t): 이전 신호로 승인된 주문을 `PaperBroker.simulate_fill(order, open(t))`로 체결 → `SettlementService.apply_fill` → Ledger. 미체결 LIMIT은 1봉 후 취소(예약 해제).
2. close(t): 종가로 평가(엄격 valuation), 평가 곡선 기록, `RiskGate.observe_equity`.
3. 전략은 bars[0..t]만 보는 `History`로 신호를 만든다. Runner가 `TradeProposal`(signal_timestamp = close(t), 기준가 = close(t))을 만들어 `StrategyGateway`로 제출한다. 마지막 봉은 신호를 내지 않는다.

- 시뮬레이션 시계가 Validator·Compliance·Integrity·Broker·Audit·Order 생성에 주입된다.
- 사이징: `position_fraction × 가용현금 / (종가 × (1+예약버퍼) × (1+스프레드·슬리피지) × (1+수수료))`, KR/US 1주·CRYPTO 0.0001 단위 내림. 공매도 없음(Long-only).
- 종료: 미청산 포지션은 종가 평가(청산하지 않음), 대기 주문은 취소·예약 해제, 모두 `end_state`에 기록.
- Reservation Breach가 발생하면 이후 모든 주문이 Risk에서 BLOCK되며 결과에 `trading_halted_by_breach`로 표시된다.

### 15.3 기준전략과 기대수익

- `ma_trend`(fast/slow SMA 교차), `breakout`(N봉 고가 돌파 / M봉 저가 이탈), `mean_reversion`(z-score).
- 기대수익(`expected_gross_return`): 기본 `empirical` — 같은 사건 유형의 **완료된 과거 사례** 평균 `close[i+h]/open[i+1]-1` (i+h ≤ t). 청산은 `-평균`. 표본 < `min_samples`면 0 + `NO_EVIDENCE`. `fixture` 모드는 검증 전용 고정값이며 결과·UI에 FIXTURE로 표시한다.

### 15.4 지표·Walk-Forward·재현성

- 지표 정의는 `qat.research.metrics` 모듈 문서와 D-026을 따른다. 거래는 flat→flat 왕복으로 재구성하며 비용은 각 Fill의 현금흐름에 한 번만 포함된다. 청산 거래 < 30이면 표본 경고.
- Cost Stress: 수수료·매도세·½스프레드·슬리피지·FX bp를 Gate와 체결 모두에서 배수로 확대.
- Walk-Forward: Fold(Train → OOS → Roll), 작은 고정 그리드(최대 12조합), Train 성과 최대 선택, Fold별 새 자본. 실패 Fold도 보존. `results/oos_registry.json`으로 재사용 여부, `results/lockbox_registry.json`으로 Lockbox 사용 이력 기록. Lockbox는 one-shot이다: 같은 `data_version`에서 **겹치는 봉 구간**을 이미 평가한 이력이 있으면(전략 무관, 구간이 달라도) 재사용으로 판정해 승인 없이는 거부하고, 승인 시 결과를 비독립으로 표시한다(감사 D8). 전략 인스턴스 상태는 실행마다 `reset()`된다(감사 D7). 전략 파라미터는 정수 ≥ 1 / 유한 실수로 검증한다(감사 D3).
- Manifest: run_id, kind, created_utc, code_commit/git_dirty(없으면 unavailable), source_tree_sha256, qat/python 버전, data(version, sha256, path, 검증상태, 기간), dataset_meta, market/symbol/timeframe/currency, strategy(name/version/params), model_version, config(전체), config_version·settings sha256(placeholder 표시), cost_models·cost_model_version, risk_overrides, initial_capital, period, seed, labels, warnings, strategy_health=NOT_IMPLEMENTED, economic_fingerprint.

### 15.5 실제 시장 데이터 기반 (Batch #3A)

**원칙**: 실제 데이터는 합성 fixture와 같은 검증 경로를 쓰되 출처·변환·판정이 재현 가능해야 연구에 들어간다. 자동 수정은 없다(보간·채움·삭제·중복 제거·OHLC 보정 금지).

- **원본(raw)**: `data/raw/<provider>/<market>/...`에 불변 저장(`xb` 배타 생성, 다른 바이트면 `RawImmutableError`). 사이드카 `*.provenance.json`에 제공자·시장·종목·조회 시각·요청/응답 범위·SHA-256·크기·형식·endpoint·타임존·`synthetic=false`를 기록한다. endpoint의 `serviceKey` 값은 항상 `<REDACTED>`.
- **정규화**: 정규 CSV `timestamp,open,high,low,close,volume`(일봉은 `YYYY-MM-DDT00:00:00`, 타임존은 메타데이터에 선언: KR `+09:00`, US·CRYPTO `UTC`). 수행한 단계만 변환 manifest에 기록(`not_performed` 목록 포함).
- **검증 V01~V18**: 헤더·필드·NaN/inf·가격≤0·OHLC·거래량·중복·순서·겹침·심볼·시장·타임존·거래일 캘린더·소스 메타데이터·synthetic 모순·보정 의미·기업행동 불연속·요청 기간 충족(V18). 종합: FAIL > UNKNOWN > PASS. V18은 요청 기간 일부를 제공자가 갖고 있지 않고 그것이 보존된 0건 응답으로 입증될 때만 WARN, 아니면 FAIL.
- **캘린더**: KR `XKRX`, US `XNYS`는 `exchange_calendars==4.13.2`로 생성한 snapshot(`data/calendars/*.json`, 자체 SHA-256). **커뮤니티 유지 데이터이며 거래소 공식 자료가 아니다.** snapshot 범위 밖은 UNKNOWN(fail-closed). CRYPTO는 24/7.
- **보정 의미**: `UNADJUSTED / SPLIT_ADJUSTED / DIVIDEND_ADJUSTED / TOTAL_RETURN_ADJUSTED / UNKNOWN`. 공식 문서 선언, 기업행동 기준일(삼성전자 50:1 2018-05-04, Apple 4:1 2020-08-31) 전후 관찰, 최종 판정을 분리해 기록하며 불명확하면 UNKNOWN(→ PASS 불가).
- **결정론적 identity**(조회 시각 미포함): `raw_set_sha256`, `normalized_sha256`, manifest·캘린더 해시, 검증 상태, `data_version = {market}-{symbol}-1d-{provider}-{norm[:12]}-{raw_set[:6]}`. 정규화를 두 번 다시 수행해 같은 해시·버전이 나와야 한다.
- **연구 투입(admission, fail-closed)**: 실제 제공자 소스는 identity(`extra.real_data`) 필수. identity 해시·메타↔identity 일치·데이터셋 바이트 해시·원본 재해시·재정규화 재현·캘린더 해시·재검증 PASS를 모두 만족해야 `run_backtest`/`run_walkforward`/`run_cost_stress`/Paper 재생이 허용된다. 실패는 `DataRejected`(도메인 오류) → UI/API HTTP 409(500 아님). 합성·로컬 파일 데이터셋은 영향 없음.
- **data.go.kr 계약(실측)**: 서비스 `GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2`(금융위원회 주식시세정보, 데이터셋 15094808). `beginBasDt`는 이상(inclusive), `endBasDt`는 **미만(exclusive)**, `numOfRows` 상한 10,000, 페이지는 하나의 내림차순 목록. 활용자 가이드에는 보정(수정주가) 명시 문장이 **없다**(필드 정의는 장중 체결 기준 값). 가이드 안내 서비스 시작일(2021-11-16)과 달리 실제 응답은 2020-01-02부터이며 그 이전은 보존된 프로브 응답이 모두 0건이다. 구(legacy) 경로 `/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo`는 동일 키에서 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`(코드 30)를 반환했고 문서화된 V2 경로는 인증에 성공했다(키·인코딩 문제가 아니라 경로 문제).
- **KR 연구 범위 계약 (Batch #3A.2, CLOSED WITH SCOPE)**: 공식 소스는 확보됨(data.go.kr 금융위원회 주식시세정보 V2). 검증된 연구 가능 기간은 **2020-01-02~2025-12-30**(005930)뿐이다. 2020년 이전(2018-01-01~2019-12-31 포함)의 공식 KR OHLCV·표현·보정 의미는 **UNKNOWN**이며 보간·backfill·합성 보충·비공식 소스 이어붙이기를 하지 않는다. Yahoo/Naver는 공식 fallback이 아니다(공식 기간에서 Naver가 MATCH였다고 Naver 2018~2019가 공식급이 되지 않는다). 연구 요청에 기간(`requested_start`/`requested_end`, YYYY-MM-DD)을 명시하면 `require_coverage`가 검증 범위와 대조한다: 범위 밖이면 `requested period exceeds verified dataset coverage`(DataRejected → HTTP 409), 범위 안이라도 더 좁은 기간은 아직 지원하지 않으므로 거부(조용한 절단·확장 없음), identity가 없는 데이터셋은 기간을 보증할 수 없어 거부. 기간 미지정이면 데이터셋 전체(= 검증 범위)를 쓴다. 연구 기간 선택은 Batch #3B 설계에서 결정한다.
- **보정 의미 표기**: KR 공식 데이터의 보정 해석은 `final=UNADJUSTED`, `assurance=UNADJUSTED_EVIDENCE_SUPPORTED`, `provider_explicit_statement=UNAVAILABLE`로 기록한다. 근거(Fact: 가이드에 보정 명시 문장 없음 / Evidence: 공식 필드 정의 + 기준가(`vs`) 일관성 1,472행 불일치 0)로 UNADJUSTED와 일치한다고 해석할 뿐 제공자의 명시적 보증이 아니다. 연구 투입은 허용한다.
- **라이선스**: 공공누리 제4유형(출처표시, 상업적 이용 금지, 변경 금지). 제3자 재배포 금지 → 원본은 로컬·gitignore.
- **자격증명**: serviceKey는 사용자 키 파일에서 API 호출 직전에만 메모리로 읽고 출력·저장하지 않는다. `tools/secret_scan.py`가 저장소 전체에서 키 텍스트 유무만 보고한다. 변경 금지: 키를 `.env`·저장소·문서·증거에 쓰지 않는다.

### 15.6 Walk-Forward 연구 프로토콜 (Batch #3B, `wf-protocol-1`)

프로토콜은 코드(`qat/research/protocol.py`)에 고정되고 결과가 나오기 전에 `artifacts/verification/walkforward_3b/protocol.json`(SHA-256 `40a7b682bf1b77355f598d52a0e63de1bf4f429c318964a25ad06a6431e2d94d`)에 기록됐다. 기존 엔진(`run_walkforward`, metrics, 전략, 탐색 공간, placeholder 비용)을 그대로 쓰며 새 전략·튜닝은 없다.

- **Fold 설계**: rolling(고정 길이 train), step = test(OOS 비중첩). train 2.0년 / OOS 0.5년 / Lockbox 1.0년 = KR·US 504/126/252 bars, CRYPTO 730/183/365 bars(연 252/365 bars). Train에서만 후보 선택(기존 계약: train net_return 최대, 동점은 grid 순서), OOS는 평가 전용이며 fold의 파라미터는 OOS를 본 뒤 바꾸지 않는다. 워밍업: 윈도 이전 봉은 지표 계산에만 쓰고 신호는 윈도 시작부터.
- **탐색 공간**: 기존 `param_grid` 그대로(ma_trend fast∈{5,10,20} × slow∈{30,50}, breakout entry∈{20,40} × exit∈{10,20}, mean_reversion window∈{20,40} × entry_z∈{1.5,2.0}). 결과를 본 뒤 확대하지 않는다.
- **비용**: `config/settings.yaml`의 **PLACEHOLDER** 값(보정·검증된 실제 비용 모델은 아직 없음, UNKNOWN). 조정하지 않았고 별도 stress(2배·3배)를 함께 기록.
- **시장별 기간(primary, `market_specific`)**: 검증된 coverage 전체 — KR 2020-01-02~2025-12-30, US 2018-01-02~2025-12-31, CRYPTO 2018-01-01~2025-12-31(identity에서 읽음). 각 시장의 마지막 Lockbox 구간은 fold·엔진 호출에서 제외(모든 엔진 호출 `trade_end ≤ 개발 구간 끝`). **Common period(secondary, `common_period`)**: 세 coverage의 교집합 2020-01-02~2025-12-30를 별도 run으로 산출하며 primary와 섞지 않고 primary 기간을 자르지 않는다(KR은 교집합과 동일 기간이라 재실행 없이 참조).
- **판정 상태**: UNKNOWN(fold 실패·fold<3·미승인·지표 결측) → INSUFFICIENT_ACTIVITY(OOS 청산 거래 합 < 30[기존 표본 경고 임계값] 또는 활동 fold < 50%) → FAIL(비용 후 stitched OOS ≤ 0) → UNSTABLE(양(+) fold가 과반 초과 아님 또는 2배 비용 stress ≤ 0) → PASS. PASS는 수익성 입증이 아니다. **Lockbox 후보** = PASS 이면서 stitched OOS가 같은 OOS 구간의 passive buy-and-hold(비용 0, 벤치마크에 유리)를 초과. 후보가 없으면 `NOT_READY_FOR_LOCKBOX`. 다중 검정 보정은 없다(9개 후보 1회씩, 효과 UNKNOWN).
- **저장**: 각 run manifest에 protocol 버전·해시·역할(primary/cost_stress/…), 데이터 identity(provider, symbol, `data_version`, raw-set·normalized SHA-256, verified coverage), 개발·Lockbox 구간, grid, fold별 선택 파라미터, 비용이 포함된다. 같은 프로토콜 버전의 재실행은 추가 OOS 열람이므로 거부된다.
- **재현성**: 9개 primary run을 `save=False`로 다시 실행해 fold별 선택 파라미터·calibration·OOS 지표·equity가 모두 일치함을 확인(`all_runs_deterministic=true`).

**실행 결과(Fact)** — 시장별 primary, Lockbox 미포함 개발/OOS 구간:

| 후보 | folds | OOS 청산 거래 | 활동 fold | 양(+) fold | stitched OOS net | 2× 비용 | 3× 비용 | passive B&H | stitched MDD | 상태 |
|---|---|---|---|---|---|---|---|---|---|---|
| KR/ma_trend | 5 | 7 | 3/5 | 0/5 | -9.9% | -8.5% | -10.1% | -10.8% | -13.2% | INSUFFICIENT_ACTIVITY |
| KR/breakout | 5 | 4 | 4/5 | 3/5 | -4.5% | -7.5% | -5.4% | -10.8% | -15.4% | INSUFFICIENT_ACTIVITY |
| KR/mean_reversion | 5 | 0 | 0/5 | 0/5 | +0.0% | +0.0% | +0.0% | -10.8% | +0.0% | INSUFFICIENT_ACTIVITY |
| US/ma_trend | 9 | 11 | 6/9 | 4/9 | +51.5% | +67.0% | +59.1% | +195.9% | -19.9% | INSUFFICIENT_ACTIVITY |
| US/breakout | 9 | 12 | 8/9 | 5/9 | +64.4% | +49.8% | +52.2% | +195.9% | -28.9% | INSUFFICIENT_ACTIVITY |
| US/mean_reversion | 9 | 6 | 4/9 | 3/9 | +8.7% | +1.5% | +0.0% | +195.9% | -12.4% | INSUFFICIENT_ACTIVITY |
| CRYPTO/ma_trend | 9 | 22 | 7/9 | 6/9 | +473.2% | +367.7% | +254.1% | +692.9% | -63.5% | INSUFFICIENT_ACTIVITY |
| CRYPTO/breakout | 9 | 14 | 8/9 | 6/9 | +562.4% | +526.6% | +455.4% | +692.9% | -56.9% | INSUFFICIENT_ACTIVITY |
| CRYPTO/mean_reversion | 9 | 7 | 2/9 | 1/9 | -0.6% | -2.9% | -10.8% | +692.9% | -29.6% | INSUFFICIENT_ACTIVITY |

Common period(secondary, 별도):

| 후보 | folds | OOS 청산 거래 | stitched OOS net | passive B&H |
|---|---|---|---|---|
| US/ma_trend | 5 | 9 | +8.0% | +30.3% |
| US/breakout | 5 | 8 | +22.0% | +30.3% |
| US/mean_reversion | 5 | 7 | +18.3% | +30.3% |
| CRYPTO/ma_trend | 5 | 11 | -8.2% | +30.3% |
| CRYPTO/breakout | 5 | 9 | +36.4% | +30.3% |
| CRYPTO/mean_reversion | 5 | 4 | -3.7% | +30.3% |

**해석(Interpretation)**: 9개 후보 모두 `INSUFFICIENT_ACTIVITY` — 일봉 추세·평균회귀 전략이 0.5년 OOS fold 5~9개에서 청산한 거래가 0~22건으로 사전 고정된 임계값(30건)에 못 미쳤다. 이것은 표본 부족(통계적 검정력)에 대한 판정이지 전략이 손실을 냈다는 뜻이 아니다. 일부 암호화폐·US 조합은 비용 후 양(+)의 stitched 수익을 보였으나 모두 passive buy-and-hold에 못 미쳤고 활동 조건도 만족하지 못했다. KR 후보는 passive(하락 구간)보다 손실이 작았지만 자체 수익은 0 이하였다. 한 시장의 결과를 다른 시장으로 일반화하지 않으며, 세 시장에서 PASS한 전략은 없다(cross-market robustness 0/3). `NOT_READY_FOR_LOCKBOX`가 현재 정상적인 연구 결과다.

참고: 비용 stress는 해당 비용 배수로 train 선택을 다시 수행하므로(비용 변화에 맞춰 파라미터가 달라짐) 2×·3× 결과가 1×보다 높게 나올 수 있다(예: US/ma_trend 2×). 이는 비용이 낮다는 뜻이 아니라 선택된 파라미터가 달라진 결과이며, 표본이 작아 해석 근거로 삼지 않는다.

**UNKNOWN**: 실제 비용 모델(placeholder), 다중 검정 보정의 영향, 더 많은 거래/더 긴 OOS가 있을 때의 결과, 사전 고정 임계값(30건·50%)의 적정성, 일봉 이외 해상도의 가용성. 수익성은 입증되지 않았다.

### 15.7 연구 타당성 기반 (Batch #3C)

목적은 성과 개선이 아니라 다음 Protocol 버전 전에 research validity를 강화하는 것이다. 전략 실행·Lockbox 접근은 없었고 증거는 `artifacts/verification/validity_3c/`에 있다.

**Batch #3B 고정(Fact)**: `wf-protocol-1`(SHA-256 `40a7b682bf1b7735…`), fold 경계, 30건·50% 임계값, 탐색 공간, OOS 결과, `NOT_READY_FOR_LOCKBOX`는 historical research evidence로 고정한다(코드가 동결 해시와 일치함을 시험으로 확인). 이 결과를 보고 임계값·grid·랭킹 규칙을 바꾸지 않으며, 같은 결과를 새로운 untouched OOS로 다시 쓰지 않는다. Batch #3B의 pre-Lockbox OOS는 이제 **observed development evidence**이고 untouched인 것은 Lockbox tail뿐이다(열지 않음).

**임계값 근거 수준**: 30건은 Batch #2의 '표본 경고'(`MIN_TRADES_FOR_STATS`)에서 온 값이며 저장소에 문헌 인용·검정력 계산이 없다 → **internal pre-registered heuristic**. 50% 활동 fold, 양(+) fold 과반 초과도 동일 분류. 옳거나 틀리다고 단정하지 않고 근거 수준만 기록한다.

**Trial registry**: trial = 한 설정(전략·파라미터)을 한 창(train 선택 또는 OOS 평가)에서 평가한 결정론적 계산. `trial_id` = 데이터 identity(`data_version`)·전략+버전·파라미터·창·단계·선택 지표·전체 base config(비용 배수 포함)·settings 해시의 SHA-256(프로토콜 버전은 기록 속성이며 식별에 포함하지 않음). 동일 계산의 재실행은 새 trial이 아니라 기존 trial에 run id만 추가한다. 이후 모든 Walk-Forward run은 manifest(`trial_context.trial_ids`)와 `results/trial_registry.json`에 등록한다(실패한 평가도 trial). 저장된 run에서 재구성한 Batch #3B(실데이터)의 실제 수: 고유 trial **1,343**(train 선택 1,106 + OOS 평가 237), 고유 설정 126, 후보 절차 9, 실행 합 1,343(재사용 trial 0). 즉 선택 편향의 관점에서 '9개 후보'가 아니라 위 수를 세어야 한다. 단, 유효 독립 trial 수는 정의하지 않았다(UNKNOWN). 비용 stress·common-period run은 별도 설정/창으로 세되 새 후보 절차로 세지 않는다. smoke·pytest의 동일 계산은 같은 id라 추가 trial이 아니다.

**다중검정/선택편향 방법 평가**(1차 문헌 확인 수준 포함): White Reality Check(Econometrica 2000)는 PDF를 받았으나 이 환경에서 본문을 읽지 못해 가정을 확인하지 못했다(출판사 초록 수준만 확인). Deflated Sharpe Ratio(Bailey·López de Prado 2014)와 PBO(Bailey·Borwein·López de Prado·Zhu)는 본문을 추출해 입력·구조를 확인했다(DSR 수식 본문은 판독 불가). 결론: (1) DSR — 독립 trial N, trial Sharpe 분산, 표본 길이, 왜도·첨도가 필요하며 9개 후보가 서로 다른 시장에 걸쳐 '같은 전략 부류' 가정과 유효 N 정의가 맞지 않고 활동 부족으로 T가 정보량을 과대 표시 → 적용 안 함. (2) PBO — 모든 설정의 전 기간 성과 행렬이 필요한데 Walk-Forward는 선택된 설정의 OOS만 저장 → 저장 결과로 계산 불가. (3) White RC — 시장별 후보 절차의 공통 시간축 OOS 시계열이 있어 구조상 가능하나 현재 활동·정상성 조건 미충족, 가정 미확인. **후보 방법(CANDIDATE_METHOD — 적용 가능성 미검증; Batch #3D에서 '선택(잠정)' 표현을 정정)**: White RC가 시장별 명시적 universe에서 유효 N 추정이 필요 없고 비모수라는 점에서 후보이나, QAT 적용에 필요한 가정을 1차 본문에서 충분히 검증하지 못했다. **이번 Batch에서는 구현하지 않았고 Batch #3B에 대한 적용 가능성은 UNKNOWN**이다. v2는 trial/후보별 OOS(필요 시 전 기간) 수익 시계열을 저장해야 한다.

**거래비용 증거**(`config/settings.yaml`의 숫자는 그대로, Batch #3B 결과도 그대로; 새 비용 모델은 향후 프로토콜 버전에서만 사용):

| 시장 | 항목 | 설정값 | 분류 | 근거 |
|---|---|---|---|---|
| KR | `commission_rate` | 0.00015 | **PLACEHOLDER** | broker/account specific; no broker is chosen |
| KR | `fx_cost_bps` | 0.0 | **EVIDENCE_SUPPORTED** | KRW-denominated asset in a KRW base currency: no currency conversion is involved (structural) |
| KR | `half_spread_bps` | 5.0 | **PLACEHOLDER** | bid-ask spread is not in daily OHLCV and no quote data was acquired; FACT: unavailable |
| KR | `slippage_bps` | 2.0 | **PLACEHOLDER** | market impact / slippage cannot be measured from daily bars; FACT: unavailable |
| KR | `tax_rate_sell` | 0.0018 | **PLACEHOLDER** | a single constant (0.18%) is applied to 2020-2025 although the official rate changes by year; the constant equals the reported 2024 total only |
| US | `commission_rate` | 0.0005 | **PLACEHOLDER** | broker specific; many brokers charge no commission; none chosen |
| US | `fx_cost_bps` | 25.0 | **PLACEHOLDER** | USD->KRW conversion cost is broker specific (and the FX rate itself is a placeholder) |
| US | `half_spread_bps` | 3.0 | **PLACEHOLDER** | no quote data acquired; FACT: unavailable |
| US | `slippage_bps` | 2.0 | **PLACEHOLDER** | not measurable from daily bars; FACT: unavailable |
| US | `tax_rate_sell` | 0.0 | **PLACEHOLDER** | configured 0.0, but an SEC Section 31 sell-side fee (rate varies by fiscal year) and a FINRA TAF exist; both are small per trade and were not modelled; only the FY2026 rates were retrieved |
| CRYPTO | `commission_rate` | 0.0005 | **PLACEHOLDER** | configured 0.05% is LOWER than the venue's current standard fee (0.10%, 0.075% with BNB): it understates cost if the user's tier is the standard one |
| CRYPTO | `fx_cost_bps` | 0.0 | **PLACEHOLDER** | USDT->KRW base conversion uses a placeholder rate; configured 0 is not evidence |
| CRYPTO | `half_spread_bps` | 8.0 | **PLACEHOLDER** | the public spot archive has no bid-ask quote data; FACT: unavailable |
| CRYPTO | `slippage_bps` | 5.0 | **PLACEHOLDER** | not measurable from klines; trade-level data exists (aggTrades/trades) but order sizes/impact are unknown; FACT: unavailable |
| CRYPTO | `tax_rate_sell` | 0.0 | **UNKNOWN** | no venue transaction tax on sales is known; tax treatment of crypto gains is jurisdiction/user dependent and is not a trading cost here |

분류 집계: VERIFIED 0, EVIDENCE_SUPPORTED 1, PLACEHOLDER 13, UNKNOWN 1. 출처: SEC Section 31 공고(1차, FY2026: 2026-04-03까지 $0.00/백만 달러, 2026-04-04부터 $20.60), FINRA 공지(Section 31 확인), 브로커 안내(FINRA TAF 2026 $0.000195/주·상한 $9.79 — 2차), 뉴스 보도(KR 증권거래세 2023~2026 — 2차, 법령 본문 미확보), Binance 수수료표(1차, 현재 시점 VIP 0 현물 0.100%/0.100%, BNB 할인 0.075%). 증권사·계좌·수수료 등급·세무 관할은 사용자 결정 없이 가정하지 않았다(UNKNOWN/의존성으로 기록). 암호화폐 수수료 설정(0.05%)은 현재 표준 수수료보다 **낮아** 비용을 과소평가할 수 있다.

**Spread/Slippage**: 일봉 OHLCV에는 호가가 없고 호가·체결 데이터를 취득하지 않았다 → **FACT: unavailable**. Binance 현물 공개 아카이브는 klines·aggTrades·trades만 제공하고 호가(bid-ask) 유형이 없다. 설정의 spread/slippage는 모두 PLACEHOLDER이며 '실제값'으로 만들지 않는다. 고빈도 데이터와 별도 미시구조 증거가 필요한 이유다.

**벤치마크 audit**: 조정 의미(같은 persisted bar)·stitching(fold별 자본 재설정)·청산 시점(마지막 OOS 봉 종가)은 일치. **불일치 발견**: v1 passive는 `open(test_start)`에서 진입하지만 엔진의 가장 이른 체결은 `open(test_start+1)`(신호는 종가, 체결은 다음 시가)로 1봉 어긋나고, 벤치마크는 100% 투자·비용 0이라 엔진(포지션 비중 0.95, 비용 있음)과 비대칭. 아래는 같은 OOS 창의 stitched passive 값이다(정렬 기준은 audit 전용 계산).

| 시장 | v1 passive | 정렬(진입 test_start+1) |
|---|---|---|
| CRYPTO | +692.9% | +629.2% |
| KR | -10.8% | -9.6% |
| US | +195.9% | +188.9% |

v1의 'beats passive' 플래그는 KR/ma_trend에서 바뀔 수 있으나 Batch #3B의 모든 판정이 `INSUFFICIENT_ACTIVITY`라 어떤 상태·Lockbox 판정도 이에 의존하지 않는다. v1은 동결 상태이므로 코드·정의를 바꾸지 않았고(정의 변경은 프로토콜 해시를 바꿈), v2는 정렬된 정의를 써야 한다. **벤치마크를 이겼다는 주장은 하지 않는다.**

**Protocol v2 데이터 요구 검토**: Batch #3B의 OOS 청산 거래는 후보당 0~22건, OOS 연당 0.0~4.88건이며 최고 비율로도 30건에 약 6.1년의 OOS가 필요하다(기획용 산정일 뿐 임계값은 낮추지 않았다). (A) 더 긴 검증 이력: KR은 공식 소스가 2020-01-02 이전에 없어 불가, US/BTC는 가능하지만 독립 관측 증가가 느리고 비용 문제 불변 → 단독으로 불충분. (B) 더 높은 빈도: BTCUSDT 1m~1d klines(체크섬)가 같은 공개 아카이브에 있어 출처 확보 가능, KR/US는 이 저장소에서 검증된 분봉 출처를 확인하지 못함; 그러나 같은 시장·같은 기간이라 레짐에 대해 독립적이지 않고, 스프레드/슬리피지·세션·타임스탬프 영향이 커지며 현재 정규 포맷·검증(V12/V13)·캘린더가 일봉 전제라 구현 부담이 큼. (C) 일봉 유지 후 종료: 비용 없음, 질문은 미해결. **판정: `PROTOCOL_V2_RECOMMENDED`** — 범위는 BTC/USDT 한정이고 아래 선결 조건이 충족되지 않으면 `RESEARCH_STOP_RECOMMENDED`. KR/US는 일봉 기준 추가 후보 연구를 중지한다. 새 untouched 증거는 Lockbox tail(열지 않음)과 검증 coverage 이후 데이터(Binance 월별 아카이브에 2026년 BTCUSDT 월이 있음; 취득에는 표준 provenance 절차와 사용자 결정이 필요)뿐이다.

필요한 새 데이터/증거(정확히):

1. USER DECISIONS: venue and fee tier (VIP level, BNB discount), whether 2026 data may serve as the new untouched holdout, whether to proceed at all
2. verified hourly (or 4h) BTCUSDT klines from the Binance archive with checksum, raw preservation, deterministic identity (extend canonical format/validation beyond daily)
3. microstructure/cost study from aggTrades/trades (trade-based spread and impact proxies) with each cost parameter classified VERIFIED / EVIDENCE_SUPPORTED / PLACEHOLDER / UNKNOWN
4. per-trial return-series storage + the trial registry so a data-snooping method (provisionally White Reality Check) can be applied; method assumptions verified against the primary text first
5. a statistical power analysis (trade count needed) fixed BEFORE any v2 result, new PROTOCOL_VERSION wf-protocol-2, aligned passive benchmark (entry open(test_start+1), same exposure and costs)

Protocol v2 Walk-Forward는 실행하지 않았다.

### 15.8 BTCUSDT 4h 기반·비용 v2·Protocol v2 사전 판정 (Batch #3D)

Protocol v2를 실행하지 않았다. 4h 전략 백테스트·파라미터 선택·2026 값 접근·Lockbox 접근은 없었다. 증거는 `artifacts/verification/intraday_3d/`.

**4h 데이터(Fact)**: Binance 공개 아카이브(data.binance.vision) spot/monthly/klines/BTCUSDT/4h의 2018-01~2025-12 월별 zip 96개 + 공식 CHECKSUM 96개를 `data/raw/_intraday_4h/`에 불변 보존(daily 데이터셋의 raw 탐색과 분리). 모든 zip의 SHA-256이 공식 checksum과 일치(불일치 0). 정규화: `timestamp`=UTC **시가** 시각 `YYYY-MM-DDTHH:MM:SS`, OHLCV, 별도 extra CSV에 UTC 시가/종가 시각·원본 시각·단위·quote volume·거래 수·taker buy·원본 파일을 행별 보존. 캘린더 없음(24/7), 모든 기대 4h 시가의 연속성을 검증. 행 17,516(기대 17,532), `data_version` `CRYPTO-BTCUSDT-4h-binance-vision-f3ab848bf53c-4d2aff`, 두 번 독립 빌드한 결과가 바이트 단위로 동일(결정론).

**타임스탬프 의미**: epoch ms(13자리) 2018~2024(15,326행), epoch µs(16자리) 2025-01-01부터(2,190행)를 행마다 자릿수로 감지해 UTC로 명시 변환하고 단위를 기록한다. 기대 단위와 모순되면 WARN(실제로 모순 0).

**검증 결과**: 무결성 검사(숫자·가격·OHLC·거래량·중복·순서·겹침·정렬·단위·심볼·출처·synthetic·checksum·holdout 경계) 전부 PASS. **완전성은 FAIL(정책 P0: 누락 봉 = FAIL, 첫 취득 전에 기록)**: 누락 4h 봉 16개(0.09%, 8개 구간: 2018-02-08(7), 2018-06-26(2), 2018-07-04(1), 2018-11-14(1), 2019-03-12(1), 2019-05-15(2), 2019-08-15(1), 2020-02-19(1)), 4h보다 짧은 봉 18개(모두 4h 미만). 거래 중단과 일치하는 패턴이지만 공식 설명은 확보하지 못했다(원인 UNKNOWN). 보간·삭제·보정 없음. 이 정책을 데이터를 본 뒤에 완화하지 않았다.

**비용 v2 계약**(Batch #3C의 분류는 동결·불변): 거래소 Binance Spot BTCUSDT, 기준 등급 Regular User·BNB 할인 없음. **VERIFIED_CURRENT**: 현재 maker 0.100% / taker 0.100%(Binance 수수료표, 1차, 2026-10-06 조회; 페이지에 수수료 이력·시행일 없음). **HISTORICAL_UNKNOWN**: 2018~2025 수수료(연구용으로 현재값을 가정하되 `ASSUMED_FOR_RESEARCH`로 라벨 — 과거 사실이 아님), 호가 스프레드, 슬리피지/시장충격. **PLACEHOLDER**: FX 비용·세금(구조적 0). **SCENARIO**: S0 0/0, S1 1/1, S2 2.5/2.5, S3 5/5 bps(half-spread/slippage) — 결과를 보기 전에 고정한 반올림 값, '관측된 비용'이 아니며 결과는 'actual-cost performance'가 아닌 시나리오 결과로만 표기. 검증 코드는 과거 값이 VERIFIED가 되거나 HISTORICAL_UNKNOWN이 값을 갖는 것을 거부한다.

**미시구조 출처 평가**: 공식 아카이브에 aggTrades·trades(·klines)가 있고 호가 데이터는 없다. 개발 구간의 하루(2023-06-11)를 체크섬 검증 후 보존해 기술 통계만 산출: aggTrades 631,628건/ trades 833,902건, 4h 봉당 거래 수 87,992~153,876(aggTrades), 거래량 중앙값 0.00201 BTC·p99 0.829 BTC, 연속 체결 가격 변화 중앙값 0.0039 bps, kline과의 봉별 거래량 대조 최대 상대오차 1.1e-13. **이 자료로 과거 bid-ask 스프레드를 관측했다고 주장하지 않는다**(체결가는 호가가 아니며 `isBuyerMaker`는 호가가 아니다; 슬리피지·충격 미도출). 현재 공개 API의 `bookTicker`·`depth`는 응답 가능(필드명만 기록, 값 폐기, 스냅샷 미저장, 수집기 미시작) — 실제 paper/live 비용 보정용 prospective 데이터는 별도 Batch.

**Per-trial series**: Walk-Forward가 평가한 모든 설정(모든 train 후보·실패 후보·OOS 평가)마다 trial id, 단계, fold, 파라미터, 창, 바 타임스탬프, 전략 수익 시계열, 정렬 벤치마크 시계열, 데이터셋 identity를 결정론적으로(정규 JSON + gzip mtime 0, trial id당 파일) 저장할 수 있다(`series_store`). 승자만 저장하는 구조는 `verify_complete`가 거부한다. 합성 fixture로 계약을 확인했다(실데이터 전략 실행 없음).

**벤치마크 v2**: 신호 봉 종가 → 다음 봉 시가 체결이라는 엔진 규칙에 맞춰 `open(start+1)`부터 노출(1봉 불일치 수정). #3B 결과·v1 코드는 그대로.

**통계적 검정력 배관 분석**(성과값 없음, 봉·fold 수와 거래 수 범위만): 4h 2y train/0.5y OOS(4,380/1,095봉) 기준 개발 구간 A(~2024-12-31, 기존 일봉 Lockbox 2025 제외) 15,326봉·9 fold·OOS 4.5년, B(~2025-12-31) 17,516봉·11 fold·OOS 5.5년. #3B의 일봉 거래 **수**(연 1.55~4.88건)에서 신호 빈도가 봉 수에 비례(6배)한다고 가정하면 A에서 후보당 42~132건으로 30건 기준을 구조적으로 넘을 수 있으나, 같은 달력 기간에 맞춘 lookback(grid×6)이면 늘지 않는다(30건 미달). 6배 가정은 측정이 아닌 **가정**이다(UNKNOWN). 30건 기준은 낮추지 않았다. 구조적 설계 가능성은 인정되나 그 자체가 경제적 우위를 의미하지 않는다.

**2026 holdout**: 개발 종료 2025-12-31, untouched 시간적 holdout 시작 2026-01-01. 코드 가드가 freeze 마커(`artifacts/verification/protocol_v2/freeze.json`) 없이는 2026 월·일 파일의 취득·파싱을 거부한다. 아카이브 메타데이터(파일명·크기·수정일)만 조회했다: BTCUSDT-4h-2026-01~2026-09(9개). 내용·값·수익률·전략 결과는 접근하지 않았다. 공개 진단: 무결성 검증이 2025년(기존 일봉 Lockbox 연도) 4h 값을 무결성 검사 목적으로 읽었으나 수익률·전략 결과는 계산하지 않았다. **미해결 설계 쟁점**: v2 개발을 2025-12-31까지 하면 일봉 Lockbox가 예약한 2025년 시장 경로를 개발에 쓰게 된다(구간 A는 이를 피함). 기존 일봉 Lockbox와 2026 holdout은 서로 다른 증거 경계다.

**설계 기록(data snooping registry)**: `design_registry.json`에 protocol 후보 id, 설계 시각, 설계 시점에 알려진 coverage, 성과 데이터 접근 여부(4h 전략 결과 NO, 2026 값 NO, #3B 일봉은 관찰됨), 결정과 이유를 기록했다.

**다중검정 방법 상태**: White Reality Check = **CANDIDATE_METHOD — APPLICABILITY UNVERIFIED**(White 2000은 specification search/data snooping 하에서 최선 모델의 벤치마크 대비 예측 우위를 평가하는 방법을 제안하나, QAT 적용 가정은 1차 본문에서 충분히 검증하지 못함). DSR·PBO 미구현(각각 부적합/전 구성 시계열 필요). 이번 Batch는 어느 방법도 구현하지 않고 필요한 시계열 보존 기반만 만들었다.

**판정: `RESEARCH_STOP_RECOMMENDED`** — 게이트: 검증된 4h 데이터 foundation 미충족(엄격 완전성 FAIL), 나머지(비용 의미 명시, trial series 저장, 벤치마크 정렬, holdout 가드, 구조적 검정력 설계 가능성)는 충족. RESEARCH_STOP은 누락/짧은 봉 게이트 하나가 유발하며 사용자가 'gap 정책(누락 봉은 거래 불가 구간으로 두고 절대 채우지 않음; freeze 문서에 기록)'을 명시적으로 수락하면 추가 데이터 작업 없이 `PROTOCOL_V2_FREEZE_READY`로 바뀔 수 있다. 이 gap 정책은 데이터를 본 뒤의 결정이므로 설계 기록에 그렇게 남긴다.

### 15.9 Protocol v2 사전등록·freeze (Batch #3E)

판정: **`PROTOCOL_V2_FROZEN`**. 전략 성과·파라미터 순위·OOS 수익·Sharpe·후보 상태·실데이터 Walk-Forward는 실행하지 않았다(합성 fixture 배관 시험만). 사양은 `artifacts/verification/protocol_v2/`(`protocol_v2.json`, `protocol_v2.sha256`, `dataset_scope.json`, `cost_contract.json`, `holdout_boundary.json`, `design_registry.json`, `freeze.json`, `freeze_verification.json`)에 정규 JSON으로 동결됐고 **protocol hash = `c1dad1503b43e58af6abb5cd711d99f8f3a51baa179d95067903c6239023c8bc`**. 이후 어떤 변경도 새 protocol version이며 조용한 수정은 `verify`가 탐지한다.

**Gap 정책(Fact)**: 2018~2025 전체 4h의 strict completeness FAIL은 유지(`missing bar = FAIL` 규칙 불변, 보간·forward fill·병합·합성·보정 없음). 부모 데이터는 `VALIDATION_FAIL_INCOMPLETE`로 남고 변경되지 않았다. 불규칙 구간은 허용된 것이 아니라 **결정적 연속 구간 규칙으로 제외**됐다: 마지막 봉에서 역방향으로 스캔하며 정확히 4h 간격·전체 4h 길이(close=open+4h−1ms)의 봉만 포함하고, 첫 불연속(누락·중복·겹침·역순·짧은 봉) 다음의 정상 봉이 `continuous_start`. 입력은 타임스탬프·봉 길이·무결성 검사뿐이며 가격·수익률·거래 수·전략 성과는 쓰지 않았다(설계 기록: **POST-DATA-INTEGRITY OBSERVATION / PRE-PERFORMANCE** — 갭을 본 뒤, 전략 결과를 보기 전에 결정).

**연구 view**: `ADMITTED_CONTINUOUS_SUBPERIOD`, `2021-09-29T08:00:00`~`2025-12-31T20:00:00`, 9,328행(기대 9,328와 동일, 누락 0), view `data_version` `CRYPTO-BTCUSDT-4h-binance-vision-cont-9a4f6ba5a9a2-f3ab84`, 모든 무결성·완전성 검사 PASS. 부모 `data_version` `CRYPTO-BTCUSDT-4h-binance-vision-f3ab848bf53c-4d2aff`·정규화/raw-set 해시·검증 상태 FAIL, 도출 규칙 `trailing-continuous-segment-v1`, 제외된 불규칙 구간(누락 8개 구간 + 짧은 봉 18개, 전부 시작점 이전)과 시작점 이전의 정상 봉 8,170개(규칙상 제외), 수정된 행 0·합성된 행 0. view는 부모 CSV의 바이트 동일 부분 슬라이스다. 가장 최근 불연속은 2021-09-29T04:00의 짧은 봉이다.

**개발 종료**: 2025-12-31(2025년은 v2 개발 증거). 기존 일봉 Lockbox는 역사적 artifact로 그대로 두며 v2 holdout으로 쓰지 않는다(변경·소비 없음).

**새 시간적 holdout**: 2026-01-01~2026-12-31, 전체 연도가 아직 끝나지 않았으므로 값을 읽지 않는다. 확인한 것은 아카이브 파일명·크기·수정일(2026-01~2026-09, 9개)뿐. 평가는 전체 2026이 준비된 뒤 별도의 사용자 승인 one-shot Batch에서 선택 절차 전체에 대해 한 번 수행한다. 가드: freeze 마커 **그리고** 별도 평가 승인 마커가 모두 있어야 2026 값을 읽을 수 있다(freeze 마커만으로는 `HoldoutAccessError`).

**비용 계약**: 현재 Binance Spot Regular User·BNB 할인 없음 maker/taker 0.100% = `VERIFIED_CURRENT`. 2018~2025 수수료 = `HISTORICAL_UNKNOWN`이며 현재값은 `ASSUMED_FOR_RESEARCH`로만 사용(역사적 사실 아님). 과거 스프레드·슬리피지 `HISTORICAL_UNKNOWN`. 엔진이 다음 봉 시가 marketable 체결과 단일 commission rate만 모델링하므로 **taker 가정**(maker 체결을 가정하지 않음). 시나리오(모두 `SCENARIO ASSUMPTION`, #3D에서 성과를 보기 전 고정): S0 0/0, S1 1/1, S2 2.5/2.5, S3 5/5 bps. S0는 plumbing·낮은 비용 진단 전용이며 파라미터 선택·후보 자격에 쓰지 않는다. 결과는 'actual-cost performance'가 아닌 시나리오 결과로만 표기.

**선택 절차(결정적 알고리즘)**: 연속 view → rolling fold(train 4,380 / OOS 1,095 / step 1,095봉, OOS 비중첩) → 기존 grid의 모든 설정(grid 순서) → 각 설정을 train에서 S1/S2/S3 모두 평가 → **S1/S2/S3 중 최악 train 점수**로 순위(동률은 grid 순서) → 선택 → OOS에서 S1/S2/S3 평가(S0 진단 기록) → 시나리오별 상태 → **후보 상태 = S1/S2/S3 중 가장 불리한 상태**. 상태 순서: UNKNOWN < INSUFFICIENT_ACTIVITY < FAIL < UNSTABLE < PASS. 자격(holdout eligible)은 최악 PASS + 모든 시나리오에서 정렬 벤치마크 초과. 목적은 실제 비용을 맞혔다는 주장이 아니라 알 수 없는 비용에 대한 시나리오 cherry-picking 방지다.

**Fold 계획**(봉 수만 사용): view 9,328봉에서 4개 fold(최소 3개 충족), OOS 2.0년(2023-09-29 ~ 2025-09-28), OOS 비중첩. 마지막 OOS 이후 1,095봉 미만의 꼬리 구간은 사용되지 않으며 창을 조정하지 않았다. #3D의 6배 거래 수 가정은 UNKNOWN이라 근거로 쓰지 않았다.

**파라미터 의미**: 저장소의 모든 전략 파라미터는 봉 수이며 달력 의미가 없다 → 기존 grid를 봉 수 그대로 사용하고 4h에서 **NEW INTRADAY HORIZON**으로 명시(예: slow=50은 4h 봉 50개 = 200시간). 일봉 달력 horizon으로 재환산(옵션 B)은 시도하지 않았다. 새 전략·grid 확장 금지.

**임계값**: OOS 청산 거래 ≥ 30, 활성 fold ≥ 50%, 양(+) fold 과반 초과, 최소 fold 3 — 모두 `INTERNAL_PRE_REGISTERED_HEURISTIC`(문헌 근거 주장 없음), Batch #3B 결과를 보고 낮추지 않았다. 벤치마크는 정렬 passive(진입 `open(start+1)`), per-trial series는 모든 평가 설정(시나리오 포함, 실패 후보 포함, 승자만 저장 금지)에 대해 저장하며 동일 trial 재실행은 trial 수를 늘리지 않는다. 다중검정: White RC = `CANDIDATE_METHOD — APPLICABILITY UNVERIFIED`, DSR·PBO 미선택, 2026 holdout이 선택 절차 전체의 독립 시간 평가 역할.

**검증된 게이트(`freeze_verification.json`)**: 연속 구간 존재, 부모 불변·FAIL 유지, fold 충분, admission 결정적, 비용 계약 동결, 벤치마크 정렬, trial series 준비, holdout 가드 활성(2026 값 접근 NO), protocol hash 동결, Lockbox 불변 — 전부 충족.

### 15.10 Protocol v2 Official Development Run #1 과 후속 동결

**상태**: `DEVELOPMENT_SELECTED_CANDIDATE — NOT_HOLDOUT_ELIGIBLE`, Protocol v2 research path **CLOSED**(§15.11; 이 문단 작성 시점의 'WAITING … USER APPROVAL' 표현은 정정됨). 결과는 모두 **development evidence**이며 수익성·알파·견고성·통계적 증명이 아니다. 통계적 검정력은 **UNKNOWN**(4 fold, OOS 약 2년).

**실행 전 고정(Fact)**: frozen Protocol v2 해시 `c1dad1503b43e58af6abb5cd711d99f8f3a51baa179d95067903c6239023c8bc` 검증 후, 성과 실행 **이전**에 immutable `candidate_selection_addendum_v1`(해시 `0f73787de09722bbf84a59ef7ba0bc3f15881749d40baf908a28aaa2af567797`, 생성 2026-10-07T01:17:16+00:00)을 기록했다. 실행 시각(2026-10-07T01:19:12+00:00)은 그보다 뒤이며 addendum은 frozen 프로토콜 해시를 건드리지 않는 별도 파일이다. 내용: PASS 후보끼리만 비교하는 결정적 순위(1 worst-case stitched OOS net return 높은 순 → 2 worst-case 최대낙폭 절대값 작은 순 → 3 worst-case OOS 청산 거래 많은 순 → 4 전략 선언 순서), 지표 정의, 실행 계약(시나리오별 settings: `costs.CRYPTO`만 교체 — taker 0.001, 세금·FX 0, 시나리오 spread/slippage; 해시 고정), 분기 규칙(A 후보 없음 / B 1개 / C 여러 개).

**실행(Fact)**: 연속 research view CRYPTO-BTCUSDT-4h-binance-vision-cont-9a4f6ba5a9a2-f3ab84에서 ma_trend·breakout·mean_reversion을 기존 grid(봉 수 그대로 = NEW INTRADAY HORIZON)로, 모든 설정을 train에서 S1/S2/S3로 평가해 worst-case 점수로 순위·선택(동률 grid 순서), 선택된 파라미터를 OOS S1/S2/S3(S0 진단)에 그대로 적용. 비용: 현재 taker 0.100%를 `ASSUMED_FOR_RESEARCH`로 사용(역사적 사실 아님), spread/slippage는 `SCENARIO ASSUMPTION`. S0는 선택·자격에 쓰지 않았다.

| 전략 | 시나리오 | stitched OOS net | stitched MDD | OOS 청산 거래 | 활성 fold | 양(+) fold | 정렬 passive | 시나리오 상태 |
|---|---|---|---|---|---|---|---|---|
| breakout | S1_low | +73.2% | -24.7% | 38 | 4/4 | 4/4 | +305.7% | PASS |
| breakout | S2_mid | +69.5% | -25.1% | 38 | 4/4 | 4/4 | +305.7% | PASS |
| breakout | S3_high | +49.0% | -25.6% | 38 | 4/4 | 3/4 | +305.7% | PASS |
| breakout | S0_commission_only (diagnostic) | +75.7% | -24.5% | 38 | 4/4 | 4/4 | +305.7% | - |
| ma_trend | S1_low | +13.0% | -30.1% | 38 | 3/4 | 2/4 | +305.7% | UNSTABLE |
| ma_trend | S2_mid | +10.6% | -30.6% | 38 | 3/4 | 2/4 | +305.7% | UNSTABLE |
| ma_trend | S3_high | +6.7% | -31.3% | 38 | 3/4 | 1/4 | +305.7% | UNSTABLE |
| ma_trend | S0_commission_only (diagnostic) | +14.7% | -29.9% | 38 | 3/4 | 2/4 | +305.7% | - |
| mean_reversion | S1_low | +19.4% | -5.7% | 15 | 1/4 | 1/4 | +305.7% | INSUFFICIENT_ACTIVITY |
| mean_reversion | S2_mid | +18.3% | -5.7% | 15 | 1/4 | 1/4 | +305.7% | INSUFFICIENT_ACTIVITY |
| mean_reversion | S3_high | +16.7% | -5.7% | 15 | 1/4 | 1/4 | +305.7% | INSUFFICIENT_ACTIVITY |
| mean_reversion | S0_commission_only (diagnostic) | +20.1% | -5.7% | 15 | 1/4 | 1/4 | +305.7% | - |

후보 상태(S1/S2/S3 중 가장 불리한 상태): ma_trend **UNSTABLE**, breakout **PASS**, mean_reversion **INSUFFICIENT_ACTIVITY**. 임계값(OOS 청산 ≥ 30, 활성 fold ≥ 50%, 양(+) fold 과반)은 변경하지 않았다. 선택된 파라미터(breakout): fold 0: {'entry_lookback': 40, 'exit_lookback': 10}; fold 1: {'entry_lookback': 40, 'exit_lookback': 20}; fold 2: {'entry_lookback': 40, 'exit_lookback': 20}; fold 3: {'entry_lookback': 40, 'exit_lookback': 20}.

**해석(Interpretation)**: breakout만 frozen 규칙상 PASS(worst-case stitched +49.0%, 최대낙폭 -25.6%, OOS 청산 38건)이므로 사전 등록한 분기 B에 따라 `DEVELOPMENT_SELECTED_CANDIDATE`로 동결했다. 그러나 같은 OOS 창에서 정렬 passive 매수보유 +305.7%(비용 0·전액 투자, 전략 평균 노출 39%)에는 모든 시나리오에서 못 미쳐 **frozen 규칙의 holdout 자격(PASS + 모든 시나리오에서 정렬 passive 초과)은 False**다. 벤치마크를 이겼다는 주장은 하지 않는다. 한 fold(fold 1)는 S3에서 음(-)이다. 표본(4 fold, 청산 38건)은 작아 우연과 구분할 수 없다(UNKNOWN). 다중검정 보정 없음(White RC 후보 방법·적용 가능성 미검증, DSR·PBO 미선택).

**Trial registry(Fact)**: 이번 실행의 평가 216회 = 고유 trial 216개(train 선택 + OOS 평가, 시나리오별), 저장된 series 216개(승자만이 아님), 전부 존재. 검증 재실행은 216회 평가했으며 series 216개가 바이트 동일하고 새로 저장된 것은 0개 — **새 trial로 세지 않음**(중복 제거). 레지스트리의 실데이터 고유 trial은 #3C의 1,343개에 이번 216개를 더한 1,559개.

**결정론(Fact)**: 재실행에서 선택 파라미터, trial id, series 해시, 지표, 상태, 선택된 후보가 모두 동일(fingerprint `463b7d00daba23bb…`).

**후보 identity**: `development_selected_candidate.json`(해시 `d038da459ad78a4a9895ea5cf1f6237c9802aa6a155d286ffb9f798d10fb9d9e`)에 전략, 절차, protocol·addendum 해시, 개발 지표 해시, trial id 해시, series 해시, fold별 선택 파라미터를 기록해 동결했다. 나머지 두 전략은 historical development 결과다.

**one-shot 2026 평가기(구현·합성 시험 완료, 실데이터 미실행)**: `holdout_evaluation_addendum_v1`(해시 `c1ad955d8cb714e5b5f20c12b7971d58751b950716f150c823ca8cdde8c803e3`)을 2026 값을 읽기 전에 고정했다. 설계: 최종 선택 창 = view의 마지막 4,380봉 → 모든 grid 설정을 S1/S2/S3로 평가해 worst-case 순위 → 선택된 파라미터를 **2026 전체 연도 한 블록**(2,190봉)에 재학습 없이 적용, 상태는 frozen 임계값(`min_folds=1`만 단일 블록 때문에 변경). 요구: 2026-01~12 전체 월 아카이브·모든 4h 봉 검증(부분 연도 평가 금지), 후보/프로토콜/addendum 해시에 묶인 사용자 승인 마커(후보가 holdout 자격이 없으면 이를 인정하는 필드 필수), exactly-once(값을 읽기 전에 attempt 파일 생성, 결과·실패 후 재시도 금지), 불변 결과 저장. 사전 공개된 위험: 개발 거래율(연 약 19건)이면 1년 블록은 30건 미만이라 `INSUFFICIENT_ACTIVITY`가 frozen 휴리스틱의 유력한 결과다(UNKNOWN). 현재 2026-01~09 월 파일명만 존재(3개월 부족), 승인 마커 없음, 값 접근 NO.

**기록 제한**: 향후 임계값·전략·grid·비용 시나리오·fold 변경은 Protocol v3로만 가능하며 이 결과를 보고 v2를 수정하지 않는다.

### 15.11 Protocol v2 종결: CLOSED / NOT_HOLDOUT_ELIGIBLE

종결 기록 `protocol_v2_closure.json`(해시 `c03aa605358656e9d4362341f3ec6a4978277cb866822165fd7f5cfc91aeaf4a`)이 상태를 구분해 기록한다.

| 항목 | 값 |
|---|---|
| development candidate | breakout |
| development status | PASS (S1/S2/S3 모두) |
| benchmark eligibility | **FAIL** — S1_low +73.2% vs +305.7%; S2_mid +69.5% vs +305.7%; S3_high +49.0% vs +305.7% (stitched OOS net vs 정렬 passive, 모든 시나리오에서 미달) |
| temporal holdout eligibility | **FALSE** (frozen 규칙: PASS **그리고** 모든 시나리오에서 정렬 passive 초과) |
| statistical power | UNKNOWN |
| Protocol v2 final state | **CLOSED / NOT_HOLDOUT_ELIGIBLE** (`DEVELOPMENT_SELECTED_CANDIDATE — NOT_HOLDOUT_ELIGIBLE`) |

**승인 경로 비활성화(Fact)**: (1) `intraday.holdout_unlocked()`는 종결 마커가 있으면 freeze·승인 마커가 위조돼 있어도 항상 False — 2026 값은 Protocol v2에서 읽을 수 없다. (2) one-shot 평가기는 holdout 자격이 없는 후보와 종결된 프로토콜의 후보를 값을 읽기 전에 `ProtocolClosed`로 거부하며, 이전의 `acknowledged_not_holdout_eligible` 승인 필드(우회 경로)는 제거됐다. 사용자 승인만으로 frozen eligibility를 우회할 수 없다. 이 위조 방지는 합성 시험·변이로 검증했다. `holdout_evaluation_addendum_v1`은 불변 문서로 보존되며 그 안의 인정(acknowledgement) 서술은 이 종결 기록이 대체한다.

**보존(historical development evidence)**: Official Development Run #1 결과·검증 재실행·trial id·trial series(해시)·후보 identity·두 addendum·frozen 사양의 파일 해시를 종결 기록에 남겼고 `verify`가 변조를 탐지한다. 성과 수치, 임계값, grid, 벤치마크 정의, 비용 시나리오, fold 설계는 변경하지 않았다. 향후 변경은 Protocol v3로만 가능하다.

**미래 사용**: one-shot 평가기 코드는 향후 프로토콜이 자체 후보·freeze·종결 상태로 재사용할 수 있는 infrastructure로 유지한다. 이 후보의 identity로는 실행할 수 없다. 2026 값과 기존 일봉 Lockbox는 읽지 않았다.

## 16. UI (Batch #2)

- 구성: `qat.ui.service.QATService`(원장·Gate·연구 결과만 읽음) + `qat.ui.server`(stdlib `ThreadingHTTPServer`, 127.0.0.1) + `qat/ui/static`(HTML/CSS/JS, CDN 없음, CSP `default-src 'self'`). 토스증권의 화면·문구·로고·아이콘을 복제하지 않은 QAT 고유 시각 체계(잉크·틸 팔레트, 카드형, tabular 숫자).
- Paper 세션 = 데이터셋 재생. 클라이언트 입력은 side/quantity/order_type/limit_price/expected_gross_return(사용자 주장)/reason_code/client_request_id만 허용하고, 그 외 필드(가격·상태·승인·종목 등)는 400으로 거부한다. `client_request_id`로 멱등 처리, 서버 lock으로 동시 요청 직렬화.
- POST는 `Content-Type: application/json` + `X-QAT-Client: ui` 헤더 필수(교차 사이트 폼 차단), 본문 64KiB 제한.
- 안전 잠금: `state/safety_latches.json`. Kill Switch 켜기와 Breach 발생 시 기록, 활성 잠금이 있으면 모든 세션이 Kill Switch 상태로 시작. Breach가 남은 세션은 새 세션으로 교체할 수 없다. 해제는 `python -m qat.ui clear-latch --id --approver --note`(오프라인)만.
- 입력 검증(감사 D2): 숫자 필드는 bool·NaN·Infinity·비정수를 400으로 거부하고 금액은 < 1e15로 제한한다. 요청 ID는 단조 카운터 기반 proposal_id와 "본 적 있는 ID 집합"으로 멱등성을 유지하며, 결과 캐시에서 밀려도 재실행하지 않는다(감사 D9).
- 요청 본문: 모든 POST는 403/415/413/400 등 **판정 전에** 한도(1 MiB) 내 본문을 소비한다(미소비 상태로 응답하면 OS가 연결을 리셋해 클라이언트가 상태 코드를 못 받을 수 있음, Batch #2.1에서 수정).
- Host/Origin(감사 D10): 루프백 바인딩이면 모든 요청의 Host가 루프백(127.0.0.1/localhost/[::1])이어야 하고, POST의 Origin은 루프백이거나 없어야 한다(DNS rebinding 방어). 비루프백 바인딩은 경고와 함께 검사를 생략한다.
- 표시 규칙: 손익은 부호 + 이익/손실 라벨, 계산 불가 값은 "산출 불가/정의 불가"(0으로 표시하지 않음), 합성·재생(STALE)·FIXTURE·placeholder 라벨, Live BLOCKED 고정, Shadow 미구현 표시. Strategy Health·R-07~09·MI-05/06은 구현되어 상태(HEALTHY/…)·NOT_CONFIGURED·데이터 미지원(UNKNOWN)을 구분해 표시한다(§19).
- **운영 화면(최종 완료)**: 운영(상태·준비도) / 감사 로그 / 복구. 가장 심각한 항목 먼저, 색 + 글자 병기, tabular 숫자, 375px·데스크톱 가로 넘침 없음. 모든 값은 서버 상태이며 클라이언트가 보낸 상태는 무시·거절한다.

## 17. 정책 결정 상태 (Control Tower)

| ID | 내용 | 현재 동작 | 영향 |
|---|---|---|---|
| OD-01 | Net Alpha Gate를 **노출 축소(청산) 주문**에 적용할지 | **CLOSED (2026-09-30)** — 노출 축소 주문은 Net Alpha 임계값만 면제 (§6.1, D-034) | 합성 fixture 청산 거절 189회/619회 → 0회. 나머지 Gate 그대로 |
| OD-02 | 허용 종목 목록 미설정 시 동작 | **CLOSED (2026-09-30)** — fail-closed(UNKNOWN). Manual/Paper는 `paper_universe`, Research는 dataset 선언 symbol을 run 범위로 (§8.1, D-035) | Live 전 실제 universe는 별도 결정(UNKNOWN 유지) |
| OD-03 | 연구 기본 예약 버퍼 2% | **CLOSED (2026-09-30)** — 2% 유지, 잠정 정책값(경험적 검증 없음) (§5.1, D-036) | 갭이 버퍼를 넘으면 Breach로 이후 주문 차단. 실제 데이터로 갭 분포 측정 후 재결정 |
| OD-04 | KR 공식 시세 데이터 소스·검증 | **CLOSED WITH SCOPE (2026-10-06, 범위 확정 Batch #3A.2)** — data.go.kr 금융위원회 주식시세정보 V2로 삼성전자(005930) 2020-01-02~2025-12-30 1,473행 검증 PASS(13개 기준 충족). **범위 한정**: 2018-01-01~2019-12-30(예상 490세션)은 공식 서비스에 없어 이 데이터셋에 **없음**(보존된 0건 응답으로 설명), 보정 의미는 공식 필드 정의 기준 UNADJUSTED(가이드에 명시 문장 없음, 해당 기간 기업행동 없음) (§15.5) | 이전 기간이 필요하면 공식 소스가 없음 — Yahoo/Naver 2018~2019는 검증 불가. 다른 종목·시장은 별도 검증 필요 |
| CLOSED | **Risk-reducing exit hierarchy** | **CLOSED (2026-09-30)** — Kill Switch 예외 없음, Daily Loss·Drawdown·절대 노출 한도는 노출 축소 SELL 허용, Reservation Breach 조건부 허용, 주문 한도 우회 불가 (§7.1, D-037~039) | 긴급 청산은 위계를 바꾸지 않는 별도의 **운영자 명시 동작**으로 구현(§19.6) |

## 18. 문서 운영 규칙

앞으로 Markdown 문서를 기능마다 새로 만들지 않는다.

- **현재 설계/정책/상태 변경** → 이 문서 수정
- **테스트/감사/결함/Batch 이력** → `QAT_검증_이력.md` 수정
- **처음 보는 사람을 위한 실행 방법** → 루트 `README.md` 수정

새 `.md` 파일은 위 세 문서로 표현할 수 없는 독립적인 장기 목적이 있을 때만 추가한다.

---

## 19. QAT v1 최종 완료 (Final Completion)

### 19.1 범위와 완료의 정의
- **COMPLETE** = 소프트웨어 / Paper / 연구 인프라의 완료. 다음은 완료와 별개이며 그대로다: **Live = BLOCKED**, **수익성 = UNKNOWN**, **Alpha = NOT PROVEN**, **Protocol v2 = CLOSED / NOT_HOLDOUT_ELIGIBLE**, **2026 holdout = UNTOUCHED**, 기존 일봉 **Lockbox = UNTOUCHED**.
- 실제 Broker 자격 증명·주문·SDK, 실거래, 새 알파 연구, 통계적 검정력 주장은 범위 밖이다.
- 상세 상태는 `artifacts/verification/final_completion/`의 gap inventory·완료 매트릭스에 있다.

### 19.2 Stale Snapshot 정책
- 세 시각을 분리한다: 스냅샷의 **소스 시각**, QAT 수신 시각, 판정 시각. 신선도 = 소스 시각의 나이. `FRESH`(나이 ≤ 최대) / `STALE`(초과) / `UNKNOWN`(소스 시각 없음·naive·미래·수신이 소스보다 앞섬·최대값 무효). UNKNOWN은 FRESH가 아니다.
- STALE/UNKNOWN 스냅샷은 대조를 통과시키지 못하고 이전 FRESH 대조 결과를 덮어쓰지도 않는다.
- Risk `snapshot_policy`: `paper_replay`(Broker 계좌 없음 — 명시 계약, 대조는 UNKNOWN 유지) / `broker_connected`(STALE → 위험 증가 BLOCK, UNKNOWN → UNKNOWN). 노출 감소 주문은 **장부가 일관되고 마지막 FRESH 대조가 일치할 때만** 허용(동결된 청산 위계와 같은 신뢰 조건). Kill Switch는 항상 최상위. 재연결은 대조를 무효화한다.
- 값은 `config/ops.yaml snapshot`(max_age_seconds, future_tolerance_seconds). broker_connected에서 max_age 누락은 설정 오류(무음 우회 없음).

### 19.3 영속 감사 로그
- `state/audit_log.jsonl`: 한 줄 = 한 이벤트, 정규화(JSON, 키 정렬), `seq`·`prev_hash`·`hash = sha256(prev_hash + 본문)`·`content_hash`. 줄 단위 append + fsync, 재시작 후 이어서 기록. `event_id` 동일·내용 동일 = 멱등, 내용 다름 = 충돌 거절.
- `verify()`가 malformed·잘림·순번 누락·체인 단절·변조·중복을 탐지. 손상된 로그는 append를 거부하고(시작 점검 BLOCK) 읽기 API는 이벤트를 내보내지 않는다. 쓰기 실패는 저장소를 FAULT로 만들고 다음 거래 동작을 거절한다(주문 처리 도중 예외를 던지지 않는다).
- 기록: 파이프라인 단계(제안 수신, 각 Gate 판정, 예약, 제출, 체결 정산, 위반), 서비스 이벤트(세션 시작, Kill Switch, 잠금, 복구), 운영자 주체. 비밀값은 저장 전 가림(`<REDACTED>`). 읽기 전용 UI/API(`/api/audit`, 화이트리스트 필터, 경로 파라미터 없음).

### 19.4 Strategy Health
- `HEALTHY / DEGRADED / UNHEALTHY / UNKNOWN`. 입력은 **운영 이벤트만**: 초기화, 데이터 시각(신선도), 평가 heartbeat/오류 연속, 신호 시각, 제안 결과(거절 집중), 상태 이월 신호. 손익·drawdown은 입력이 아니다. 거래 없음은 실패가 아니라 메모다. 관찰만 하며 전략을 바꾸거나 끄지 않는다(§12.2, 자동 재학습·교체 금지). 임계값은 `config/ops.yaml strategy_health`.

### 19.5 R-07/R-08/R-09, MI-05/MI-06 (정확한 정의)
설계 §7·§8.2는 이름만 주므로 의미와 필요한 데이터를 여기서 고정한다. 임계값이 `null`이면 **NOT_CONFIGURED**(규칙은 있으나 비활성, 화면에 그렇게 표시, PASS 아님).

| 규칙 | 정의 | 데이터 | 비고 |
|---|---|---|---|
| R-07 | 기준 봉 거래량 ≤ 0 또는 < 최소 → 위험 증가 주문 BLOCK, 거래량 불명 → UNKNOWN | 봉 거래량 | 현재 데이터로 지원 |
| R-08 | 호가 스프레드(bp) > 한도 → BLOCK | **호가** | OHLCV에는 호가가 없어 설정 시 UNKNOWN(PASS 아님) |
| R-09 | (고가−저가)/종가 > 한도 → BLOCK | 봉 OHLC | 대응 = 위험 증가 주문 차단(자동 청산 아님) |
| MI-05 | 서버가 기록한 심볼별 취소 횟수(창 내) > 한도 → BLOCK | 서버 취소 이력 | 클라이언트가 이력에 관여 불가 |
| MI-06 | 주문 수량 / 기준 봉 거래량 > 한도 → BLOCK, 거래량 불명 → UNKNOWN | 봉 거래량 | 모든 주문에 적용 |

R-07/08/09는 위험 증가 주문에만 적용하고 노출 감소 SELL은(장부가 신뢰될 때) 막지 않는다 — **동결된 청산 위계(§7.1)를 바꾸지 않는다**. Kill Switch는 최상위 정지. 컨텍스트는 서버가 검증된 데이터셋에서 공급한다.

### 19.6 긴급 청산(Emergency Flatten)과 복구 상태 기계
- 자동 실행 경로는 없다. 운영자 이름 + 사유 + 확인 문구 `FLATTEN`이 모두 있어야 한다. 서버가 장부로 수량·가격·방향을 결정하며 클라이언트가 보낸 다른 필드는 거절한다.
- 장부를 신뢰할 수 없으면(무결성 문제, 대조 불일치, 비정상 Breach, 비유한 값, 평가가격 없음) **아무것도 매도하지 않고** `RECOVERY_REQUIRED`로 전환한다(추측 청산 금지).
- 허용되면: 미체결 주문 취소(예약 해제) → 보유 롱을 **보유 수량 이하**로 시장가 매도(추가 노출·반대 포지션 거절). 일반 파이프라인과 분리된 `submit_recovery_order`를 쓰며 권한 토큰은 복구 컨트롤러만 발급한다(128비트 토큰 등록부; 위조 불가). Validator·회계 무결성·노출 감소 판정·Compliance는 그대로 통과해야 하고, Kill Switch는 **해제되지 않는다**. 멱등(재요청 시 기록 반환), 전 단계 감사.
- 상태: `NORMAL → FLATTEN_SUBMITTED → FLATTENED`, 신뢰 불가 시 `RECOVERY_REQUIRED`. 영속(원자적 쓰기). `RECOVERY_REQUIRED`·`FLATTEN_SUBMITTED`는 새 위험·새 세션을 막고 해제는 오프라인 `ack-recovery`(감사 기록).
- 재시작 평가: `FIRST_START / SAFE_RECOVERY / MANUAL_INTERVENTION_REQUIRED`(사유 전부 표시). 조용한 초기화는 없다 — 손상된 안전 잠금 파일은 격리 후 `STATE_CORRUPT` 잠금(Kill Switch 유지)으로 대체되고, 다른 손상 상태 파일은 시작 점검 BLOCK이다.

### 19.7 Broker 어댑터 경계와 실패 의미론
- `qat.brokerage`: `BrokerAdapter` 계약(스냅샷·포지션·현금·제출(멱등 `client_order_id`)·취소·상태·체결·재연결, 소스 시각), `FakeBrokerAdapter`(결정론적 장애 주입), `BrokerBoundary`(라우터/코디네이터가 쓰는 Broker 인터페이스 구현). SDK·자격 증명·네트워크 없음. 어댑터는 Risk/Compliance 판단을 하지 않는다.
- 실패 의미론: timeout / 연결 끊김 → 주문 `UNKNOWN`(**종결 상태 아님**: 예약 유지, 회계 무결성 이슈 기록, 신규 노출 거절). 명시적 거부 → 거부 확정 + 예약 해제. 중복 응답·체결 콜백은 `broker_fill_id`로 제거. 체결은 검증(주문 존재·방향·유한 양수·누적 ≤ 주문 수량·종결 주문 아님) 후 정산, 불일치는 적용하지 않고 무결성 이슈 기록. stale/UNKNOWN 스냅샷은 대조를 통과시키지 못한다. 재연결은 대조를 무효화하고 UNKNOWN 주문을 재조회한다. 확인되지 않은 취소는 예약을 풀지 않는다.
- 실제 어댑터는 구현하지 않았다(첫 Broker UNKNOWN; Live BLOCKED).

### 19.8 Paper 졸업 평가 / Live 준비도
- **Paper 졸업**(`GRADUATED / NOT_READY / BLOCKED / UNKNOWN`)은 Paper 시스템의 **운영 준비도**다. Alpha·수익성·Live 준비를 뜻하지 않는다. 우선순위 BLOCKED > NOT_READY > UNKNOWN > GRADUATED. 조건: G1 운영 무결성(시작 점검·감사·잠금·회계), G2 Paper 세션 수, G3 체결 수, G4 복구 훈련 증거, G5 검증된 전략(Protocol v2 `NOT_HOLDOUT_ELIGIBLE`이면 정직하게 FAIL), G6 비용 보정(placeholder → UNKNOWN), G7 Broker 대조 증거(없음 → UNKNOWN), G8 Strategy Health. 기준 수치는 소유자 미정의 → `config/ops.yaml paper_graduation`의 **잠정 공학 수치**. 현재 결과: **NOT_READY**.
- **Live 준비도**: 항상 **BLOCKED**. 라우터 LIVE 거절과 Live Broker stub 거절을 실제로 호출해 확인하며, UI·설정·증거 입력으로 바꿀 수 없다. 해제 경로는 이 빌드에 없다.

### 19.9 운영 상태 요약 · 시작 점검 · 설정 검증
- 구성요소: Data, Ledger, Reconciliation, Risk, Compliance, Integrity, Strategy Health, Audit, Broker, Startup, Recovery + 참고(Research, Paper, Live). 상태 `PASS / DEGRADED / BLOCKED / UNKNOWN / NOT_CONFIGURED`. NOT_CONFIGURED·UNKNOWN은 PASS로 승격되지 않으며, 참고 항목(Live BLOCKED 등)은 종합 상태를 결정하지 않는다. Paper 재생은 Broker 계좌가 없어 Reconciliation = UNKNOWN.
- 시작 점검: 설정 유효, 디렉터리 쓰기, 잠금 로드, 복구/안전 상태 읽기, 감사 체인, Protocol v2 증거 해시(2026 값·Lockbox 미접근), 미지원 Live 설정, Broker 대조 요건. BLOCK이면 세션 시작·주문·재생 진행을 거절(fail-closed). Kill Switch는 항상 동작.
- `config/ops.yaml` 검증: 유한 수·범위·snapshot 정책·paper/live 모드·임계값. `mode: live`는 설정으로 허용되지만 시작 점검이 거절한다.

### 19.10 상태 저장
- 잠금·복구·안전 상태·연구 결과 JSON/registry는 임시 파일 + fsync + `os.replace`로 교체한다(중간 실패 시 이전 파일 유지). 손상은 감지하며 빈 상태로 읽지 않는다(예: 손상된 OOS registry → `StateCorrupt`).
- **단일 프로세스 전용**: 두 프로세스가 같은 상태 디렉터리를 쓰는 구성은 지원하지 않는다(잠금·합의 미구현).
- protocol/addendum/candidate/closure 파일은 해시 검증되는 불변 historical evidence이며 이번 작업에서 변경하지 않았다.

### 19.11 추가 UI/API
`GET /api/operations`, `GET /api/audit?offset&limit&session_uid&event_type&order`, `GET /api/recovery`, `POST /api/recovery/flatten {operator, reason, confirm}`. POST는 기존과 같은 신뢰 경계(루프백 Host/Origin, `X-QAT-Client: ui`, JSON, 64KiB). Live를 켜는 라우트는 없다. CLI: `python -m qat.ui ack-recovery --approver --note`.

### 19.12 정직한 한계
Paper Broker는 현실 체결이 아니다. 비용·세율·FX·위험 한도·R-07~MI-06 임계값·졸업 기준 수치는 placeholder/잠정값이다. Strategy Health는 운영 상태이지 성과가 아니다. Broker 경계는 계약 시험으로만 검증되었고 실제 Broker와는 검증되지 않았다.
