import Foundation

enum APIError: LocalizedError {
    case invalidURL
    case httpError(Int)
    case decodingError(Error)
    case networkError(Error)

    var errorDescription: String? {
        switch self {
        case .invalidURL: return "Invalid URL"
        case .httpError(let code): return "HTTP error \(code)"
        case .decodingError(let e): return "Decoding error: \(e.localizedDescription)"
        case .networkError(let e): return e.localizedDescription
        }
    }
}

class APIService {
    static let shared = APIService()
    private let baseURL = Config.backendURL

    private func makeRequest(_ path: String, method: String = "GET") -> URLRequest? {
        guard let url = URL(string: baseURL + path) else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue(Config.apiKey, forHTTPHeaderField: "X-API-Key")
        return req
    }

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        // Custom date strategy: handles multiple ISO 8601 variants from different RSS feeds
        // e.g. "2026-05-11T12:00:00Z", "2026-05-11T12:00:00+00:00", "2026-05-11T12:00:00.000Z"
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let isoStrict = ISO8601DateFormatter()
        isoStrict.formatOptions = [.withInternetDateTime]
        d.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let str = try container.decode(String.self)
            if let date = iso.date(from: str) { return date }
            if let date = isoStrict.date(from: str) { return date }
            throw DecodingError.dataCorruptedError(in: container,
                debugDescription: "Cannot decode date: \(str)")
        }
        return d
    }()

    func fetch<T: Decodable>(_ path: String, method: String = "GET", timeout: TimeInterval = 10) async throws -> T {
        guard let url = URL(string: baseURL + path) else { throw APIError.invalidURL }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = timeout
        request.setValue(Config.apiKey, forHTTPHeaderField: "X-API-Key")

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw APIError.networkError(error)
        }

        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw APIError.httpError(http.statusCode)
        }

        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.decodingError(error)
        }
    }

    func getAccount() async throws -> AccountInfo {
        try await fetch("/api/account")
    }

    func getPositions() async throws -> [Position] {
        try await fetch("/api/positions")
    }

    func getOrders() async throws -> [Order] {
        try await fetch("/api/orders")
    }

    func getTradingStatus() async throws -> TradingStatus {
        try await fetch("/api/trading/status")
    }

    func getAIAnalysis() async throws -> AIAnalysis {
        try await fetch("/api/trading/analysis")
    }

    func getRecentAIDecisions(limit: Int = 20) async throws -> [BotActivity] {
        try await fetch("/api/trading/analysis/recent?limit=\(limit)")
    }

    func startTrading() async throws {
        let _: [String: String] = try await fetch("/api/trading/start", method: "POST")
    }

    func stopTrading() async throws {
        let _: [String: String] = try await fetch("/api/trading/stop", method: "POST")
    }

    func getNews() async throws -> [NewsArticle] {
        try await fetch("/api/news")
    }

    func getPortfolioHistory(period: String = "1W") async throws -> [PortfolioPoint] {
        try await fetch("/api/portfolio/history?period=\(period)")
    }

    func getPerformance() async throws -> PerformanceStats {
        try await fetch("/api/performance")
    }

    func getLongShortScorecard() async throws -> [String: SideScorecard] {
        try await fetch("/api/performance/scorecard")
    }

    func getBlockedTradesReport() async throws -> [BlockedTradeReport] {
        try await fetch("/api/performance/blocked-trades")
    }

    func getPortfolioVaR() async throws -> PortfolioVaR {
        try await fetch("/api/performance/var")
    }

    func getPerformanceBySetup() async throws -> [SetupPerformance] {
        try await fetch("/api/performance/by-setup")
    }

    func getAIBaselineStats() async throws -> AIBaselineStats {
        try await fetch("/api/performance/ai-baseline")
    }

    func getSlippageSummary() async throws -> SlippageSummary {
        try await fetch("/api/performance/slippage")
    }

    func getPerformanceByHour() async throws -> [HourPerformance] {
        try await fetch("/api/performance/by-hour")
    }

    func getStrategy() async throws -> String {
        let response: [String: String] = try await fetch("/api/strategy/")
        return response["key"] ?? "aggressive"
    }

    func setStrategy(_ key: String) async throws {
        let _: [String: String] = try await fetch("/api/strategy/set/\(key)", method: "POST")
    }

    func getAllStrategies() async throws -> [[String: String]] {
        try await fetch("/api/strategy/all")
    }

    func getSuggestions(refresh: Bool = false) async throws -> [Suggestion] {
        let response: SuggestionsResponse = try await fetch("/api/predictions/suggestions\(refresh ? "?refresh=true" : "")", timeout: 90)
        return response.suggestions
    }

    func getStockPrediction(symbol: String) async throws -> StockPrediction {
        try await fetch("/api/predictions/\(symbol)", timeout: 90)
    }

    /// Real-time bid/ask midpoint from Alpaca — always fresh, never cached.
    func getLivePrice(symbol: String) async -> Double? {
        struct PriceResponse: Decodable { let price: Double }
        return try? await (fetch("/api/predictions/price/\(symbol)", timeout: 5) as PriceResponse).price
    }

    /// Batch real-time prices for multiple symbols in one round-trip.
    func getLivePrices(symbols: [String]) async -> [String: Double] {
        struct BatchResponse: Decodable { let prices: [String: Double] }
        guard !symbols.isEmpty else { return [:] }
        let joined = symbols.joined(separator: ",")
        guard let encoded = joined.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) else { return [:] }
        let response = try? await (fetch("/api/predictions/prices?symbols=\(encoded)", timeout: 8) as BatchResponse)
        return response?.prices ?? [:]
    }

    /// Resolve a company name or partial ticker to a list of matching ticker symbols.
    /// e.g. "Apple" → [TickerResult(symbol: "AAPL", name: "Apple Inc.", exchange: "NASDAQ")]
    func searchTicker(query: String) async throws -> [TickerResult] {
        guard let encoded = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) else { return [] }
        let response: TickerSearchResponse = try await fetch("/api/predictions/search?q=\(encoded)", timeout: 10)
        return response.results
    }

    /// Convenience: returns the top ticker symbol for a query, or nil if nothing found.
    func resolveToTicker(_ input: String) async -> String? {
        let trimmed = input.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        // If it already looks like a ticker (short, no spaces), use it directly
        if trimmed.count <= 5 && !trimmed.contains(" ") && trimmed == trimmed.uppercased() {
            return trimmed
        }
        // Otherwise search by company name
        if let results = try? await searchTicker(query: trimmed), let first = results.first {
            return first.symbol
        }
        return trimmed.uppercased() // fallback: treat as ticker
    }

    func getDailyPicks(refresh: Bool = false) async throws -> DailyPicksResponse {
        try await fetch("/api/picks/daily\(refresh ? "?refresh=true" : "")", timeout: 120)
    }

    func cancelOrder(id: String) async throws {
        guard var request = makeRequest("/api/orders/\(id)", method: "DELETE") else { throw APIError.invalidURL }
        request.timeoutInterval = 10
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw APIError.networkError(NSError(domain: "", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: detail ?? "Cancel failed"]))
        }
    }

    func placeManualOrder(symbol: String, side: String, qty: Int) async throws {
        guard var request = makeRequest("/api/orders/manual", method: "POST") else { throw APIError.invalidURL }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 15
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["symbol": symbol, "side": side, "qty": qty])
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw APIError.networkError(NSError(domain: "", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: detail ?? "Order failed"]))
        }
    }

    func getWatchlist() async throws -> [String] {
        let response: [String: [String]] = try await fetch("/api/watchlist/")
        return response["watchlist"] ?? []
    }

    func setWatchlist(_ symbols: [String]) async throws {
        guard var request = makeRequest("/api/watchlist/", method: "POST") else { throw APIError.invalidURL }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["watchlist": symbols])
        let (_, _) = try await URLSession.shared.data(for: request)
    }

    func getRiskSettings() async throws -> RiskSettings {
        return try await fetch("/api/risk/settings")
    }

    func setRiskSettings(_ settings: RiskSettings) async throws {
        guard var request = makeRequest("/api/risk/settings", method: "POST") else { throw APIError.invalidURL }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.httpBody = try? JSONEncoder().encode(settings)
        let (_, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw APIError.httpError(http.statusCode)
        }
    }

    func getPositionHistory(limit: Int = 50) async throws -> [PositionLog] {
        try await fetch("/api/positions/history?limit=\(limit)")
    }

    func getPerformanceSummary() async throws -> PerformanceSummary {
        try await fetch("/api/positions/performance")
    }

    func getWatermarks() async throws -> [String: Double] {
        let response: [String: [String: Double]] = try await fetch("/api/positions/watermarks")
        return response["watermarks"] ?? [:]
    }

    func getBotActivity(limit: Int = 50) async throws -> [BotActivity] {
        try await fetch("/api/trading/activity?limit=\(limit)")
    }

    func getEODReport() async throws -> EODReport {
        try await fetch("/api/eod/latest")
    }

    func triggerEODAnalysis() async throws {
        let _: [String: String] = try await fetch("/api/eod/run", method: "POST")
    }

    // ── Trading Budget ──
    func getTradingBudget() async throws -> BudgetStatus {
        try await fetch("/api/risk/budget")
    }

    func setTradingBudget(_ amount: Double?) async throws {
        guard var request = makeRequest("/api/risk/budget", method: "POST") else { throw APIError.invalidURL }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        if let amount = amount {
            request.httpBody = try? JSONSerialization.data(withJSONObject: ["amount": amount])
        } else {
            request.httpBody = try? JSONSerialization.data(withJSONObject: ["amount": NSNull()])
        }
        let (_, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw APIError.httpError(http.statusCode)
        }
    }

    // ── Prompt Viewer + Override ──
    func getLastPrompts() async throws -> PromptData {
        try await fetch("/api/prompt/last")
    }

    func getPromptOverride() async throws -> PromptOverrideStatus {
        try await fetch("/api/prompt/override")
    }

    func setPromptOverride(_ text: String?) async throws {
        guard var request = makeRequest("/api/prompt/override", method: "POST") else { throw APIError.invalidURL }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        if let text = text, !text.isEmpty {
            request.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text])
        } else {
            request.httpBody = try? JSONSerialization.data(withJSONObject: ["text": NSNull()])
        }
        let (_, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw APIError.httpError(http.statusCode)
        }
    }

    // ── Finance: Tax summary ──
    func getTaxSummary(bracketRate: Double = 0.22) async throws -> TaxSummary {
        try await fetch("/api/finance/tax-summary?bracket_rate=\(String(format: "%.2f", bracketRate))")
    }

    // ── Finance: Profit reserve ──
    func getReserve() async throws -> ReserveStatus {
        try await fetch("/api/finance/reserve")
    }

    func resetReserve(amount: Double? = nil) async throws -> ReserveResetResponse {
        guard var request = makeRequest("/api/finance/reserve/reset", method: "POST") else { throw APIError.invalidURL }
        var body: [String: Any] = ["confirm": true]
        if let amt = amount { body["amount"] = amt }
        request.timeoutInterval = 15
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw APIError.httpError(http.statusCode)
        }
        return try decoder.decode(ReserveResetResponse.self, from: data)
    }

    // ── AI Model Settings ──
    func getModelSettings() async throws -> ModelSettings {
        try await fetch("/api/settings/models")
    }

    // ── Session 7: Near-Miss Tracker + Sprint Review ──
    func getNearMisses() async throws -> NearMissReport {
        try await fetch("/api/performance/near-misses")
    }

    func getSprintReviewLatest() async throws -> DailySprintReview {
        try await fetch("/api/performance/sprint-review/latest")
    }

    func updateModelSettings(_ update: ModelSettingsUpdate) async throws {
        guard var request = makeRequest("/api/settings/models", method: "POST") else { throw APIError.invalidURL }
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.httpBody = try? JSONEncoder().encode(update)
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            // Surface the backend's "detail" message if available
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw APIError.networkError(NSError(domain: "", code: http.statusCode,
                userInfo: [NSLocalizedDescriptionKey: detail ?? "HTTP error \(http.statusCode)"]))
        }
    }
}
