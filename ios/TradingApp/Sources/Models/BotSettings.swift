import Foundation

// ── Trading Budget ──────────────────────────────────────────────────────────

struct BudgetStatus: Decodable {
    let trading_budget: Double?
    let portfolio_value: Double?
    let using_full_portfolio: Bool
}

// ── Prompt Viewer + Override ────────────────────────────────────────────────

struct PromptData: Decodable {
    let available: Bool
    let saved_at: String?
    let step1: String?
    let step2: String?
    let message: String?
}

struct PromptOverrideStatus: Decodable {
    let override: String?
    let active: Bool
}
