# Lógica de Negocio - Poly-Oracle-Agent

Este documento establece las reglas fundamentales que rigen las decisiones del agente para la ejecución de órdenes de compra.

## 1. Regla de Compra (Buy Order Rule)

El bot **solo** está autorizado a ejecutar una operación de compra si el **Valor Esperado (EV)** es estrictamente positivo (**EV > 0**).

### 1.1. Fórmula del Valor Esperado

El cálculo del Valor Esperado se basa en la probabilidad de éxito y fracaso de la operación:

$$\text{EV} = (P_{\text{win}} \times \text{Profit}) - (P_{\text{loss}} \times \text{Loss})$$

**Donde:**

| Parámetro | Descripción |
| :--- | :--- |
| $P_{\text{win}}$ | **Probabilidad de Ganar**: Probabilidad estimada de que la operación genere beneficios (0 a 1). |
| $\text{Profit}$ | **Ganancia Esperada**: El rendimiento neto estimado en caso de éxito. |
| $P_{\text{loss}}$ | **Probabilidad de Perder**: Probabilidad estimada de que la operación genere pérdidas (0 a 1). Nota: Generalmente $P_{\text{loss}} = 1 - P_{\text{win}}$. |
| $\text{Loss}$ | **Pérdida Esperada**: La pérdida neta estimada en caso de fallo (por ejemplo, stop-loss o liquidación). |

---

## 2. Condición de Activación

$$\text{Acción} = \text{COMPRA} \iff \text{EV} > 0$$

> [!IMPORTANT]
> Ningún otro factor (sentimiento, volumen solitario, etc.) puede anular esta regla sin que el cálculo del EV esté presente y sea positivo.
