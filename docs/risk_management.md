# Gestión de Riesgos (Risk Management Specification) - Poly-Oracle-Agent

**Versión:** 1.0.0
**Estado:** Referencia Canónica
**Alcance:** Define todas las reglas de riesgo cuantitativas aplicadas entre el Nodo de Evaluación LLM (Capa 3) y el Nodo de Ejecución Web3 (Capa 4).

---

## 1. Modelo Mental

Cada operación que el agente considera es un contrato de resultado binario donde:

$$\text{YES} + \text{NO} = \$1.00 \text{ USDC (totalmente colateralizado)}$$

Esto significa que nunca estamos "comprando probabilidad" en sentido abstracto; estamos adquiriendo un derecho contingente al precio $p_{\text{market}}$ (la probabilidad implícita del mercado) que paga $\$1.00$ si el resultado se resuelve como YES, o $\$0.00$ en caso contrario.

El único **margen estructural (edge)** del agente es cuando su probabilidad estimada real $p_{\text{true}}$ (proveniente del LLM) diverge significativamente de $p_{\text{market}}$ (el punto medio del CLOB). Todas las reglas de riesgo están diseñadas para capturar ese margen sobreviviendo al error de estimación inherente al pronóstico basado en LLM.

---

## 2. Fórmula Core: Criterio de Kelly Fraccional

### 2.1 Kelly Completo (Forma de Mercado de Predicción Binaria)

Para un contrato YES/NO que cuesta $p_{\text{market}}$ y paga $\$1.00$ al resolverse, las cuotas netas $b$ (beneficio por $\$1$ apostado en caso de ganar) son:

$$b = \frac{1 - p_{\text{market}}}{p_{\text{market}}}$$

La fracción de Kelly canónica del capital (bankroll) es:

$$f^* = \frac{b \cdot p_{\text{true}} - q}{b}$$

**Donde:**
*   $p_{\text{true}}$: Probabilidad real estimada por el LLM $[0.01, 0.99]$.
*   $p_{\text{market}}$: Mejor oferta de compra (best-ask) del CLOB (costo de 1 acción YES) $(0.0, 1.0)$.
*   $q$: Probabilidad de perder, $q = 1 - p_{\text{true}}$.
*   $b$: Cuotas netas, $b = (1 - p_{\text{market}}) / p_{\text{market}}$.

> [!IMPORTANT]
> **Distinción Crítica: $EV \neq f^*$** (excepto cuando $p_{\text{market}} = 0.5$)
> *   $EV = p_{\text{true}} \cdot b - q \implies$ Tasa de retorno por $\$1$ apostado.
> *   $f^* = (b \cdot p_{\text{true}} - q) / b \implies$ Fracción del capital a asignar.

**Ejemplo a $p_{\text{market}} = 0.60$ y $p_{\text{true}} = 0.70$:**
*   $b = 0.40 / 0.60 = 0.6667$
*   $EV = 0.70 \times 0.6667 - 0.30 = 0.1667$ ($16.67\%$ de retorno por $\$1$)
*   $f^* = 0.1667 / 0.6667 = 0.2500$ ($25.0\%$ del capital)

---

### 2.2 Quarter-Kelly (Multiplicador Aplicado)

Kelly completo es óptimo bajo conocimiento perfecto de la probabilidad, pero maximiza la varianza severamente. Las estimaciones de LLM conllevan incertidumbre de modelo.

Se aplica un factor de **$0.25\times$ (Quarter-Kelly)** para mitigar el ruido en la estimación del margen:

$$f_{\text{quarter}} = 0.25 \times f^*$$

**Continuando el ejemplo:**
$$f_{\text{quarter}} = 0.25 \times 0.25 = 0.0625 \implies \text{Arriesgar } 6.25\% \text{ del capital.}$$

---

### 2.3 Valor Esperado (EV)

Antes de aplicar el tamaño de Kelly, el EV debe ser positivo. El EV es la puerta principal:

$$EV = p_{\text{true}} / p_{\text{market}} - 1$$

*   $EV > 0 \iff p_{\text{true}} > p_{\text{market}}$ (el agente ve un margen positivo)
*   $EV \leq 0 \implies$ **HOLD FORZADO** (sin operación)

---

## 3. Filtros de Seguridad de Hardware (Pre-Execution Gate)

Todos los filtros deben pasar simultáneamente para que `decision_boolean = True`.

### Filtro 1 — Puntuación Mínima de Confianza del LLM

*   **LÍMITE:** $\text{confidence\_score} \geq 0.75$
*   **Motivación:** Kelly asume una estimación fiable. Por debajo del $75\%$, el cálculo carece de fundamento suficiente.

### Filtro 2 — Spread Máximo del Orderbook

*   **LÍMITE:** $\frac{\text{best\_ask} - \text{best\_bid}}{\text{best\_ask}} \leq 0.015 \quad (1.5\%)$
*   **Motivación:** Spreads amplios indican liquidez delgada; entrar destruye el EV por costos implícitos.

### Filtro 3 — Exposición Máxima por Operación

*   **LÍMITE:** $\text{position\_size\_usdc} \leq 0.03 \times \text{total\_bankroll\_usdc}$
*   **Motivación:** Incluso tras Kelly, la exposición absoluta se capa al $3\%$ para evitar caídas catastróficas por eventos adversos.

$$\text{position\_size\_usdc} = \min(f_{\text{quarter}} \times \text{bankroll}, 0.03 \times \text{bankroll})$$

### Filtro 4 — Umbral de Valor Esperado Mínimo

*   **LÍMITE:** $EV > 0.02 \quad (2\% \text{ de margen mínimo})$
*   **Motivación:** Margenes ínfimos son devorados por gas, comisiones y slippage.

### Filtro 5 — Tiempo Mínimo para Resolución

*   **LÍMITE:** $\text{market\_end\_date} > \text{NOW} + 4 \text{ horas}$
*   **Motivación:** Mercados cercanos a la resolución sufren volatilidad extrema donde el LLM es poco fiable.

---

## 4. Matriz de Decisión del Gatekeeper

| EV | Confianza | Spread | Exposición | Tiempo Guard | Resultado |
| :--- | :--- | :--- | :--- | :--- | :--- |
| $> 2\%$ | $\geq 0.75$ | $\leq 1.5\%$ | $\leq 3\%$ | $> 4h$ | ✅ **EXECUTE** |
| $\leq 0$ | any | any | any | any | 🚫 **HOLD FORZADO** |
| $> 0$ | $< 0.75$ | any | any | any | 🚫 **HOLD (conf)** |
| $> 0$ | $\geq 0.75$ | $> 1.5\%$ | any | any | 🚫 **HOLD (liq)** |
| $> 0$ | $\geq 0.75$ | $\leq 1.5\%$ | $> 3\%$ | any | ⚠️ **SIZE DOWN** |
| $> 0$ | $\geq 0.75$ | $\leq 1.5\%$ | $\leq 3\%$ | $\leq 4h$ | 🚫 **HOLD (time)** |

---

## 5. Registro de Parámetros de Riesgo

| Parámetro | Símbolo | Valor | Configurable |
| :--- | :--- | :--- | :--- |
| Kelly multiplier | `KELLY_FRAC` | $0.25$ | Sí (.env) |
| Min confidence score | `MIN_CONF` | $0.75$ | Sí (.env) |
| Max spread (%) | `MAX_SPREAD` | $0.015$ | Sí (.env) |
| Max single exposure (%) | `MAX_EXPOSURE` | $0.03$ | Sí (.env) |
| Min EV threshold | `MIN_EV` | $0.02$ | Sí (.env) |
| Min time-to-resolution (h) | `MIN_TTR_H` | $4$ | Sí (.env) |

Todos los parámetros se gestionan en `src/core/config.py` vía `pydantic-settings`.

---

## 6. Auditoría de Riesgo (Risk Audit Trail)

Cada decisión —incluyendo el filtro que causó un HOLD— se registra en `AgentDecisionLog.reasoning_log` con el prefijo:

`[GATEKEEPER] HOLD | filter=MIN_CONFIDENCE | value=0.68 | threshold=0.75`
