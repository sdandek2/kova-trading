import Foundation

struct TradingStatus: Codable {
    let isRunning: Bool
    let lastAnalysisAt: Date?
    let nextRunInSeconds: Int?

    enum CodingKeys: String, CodingKey {
        case isRunning = "is_running"
        case lastAnalysisAt = "last_analysis_at"
        case nextRunInSeconds = "next_run_in_seconds"
    }
}

struct AIAnalysis: Codable {
    let reasoning: String
    let lastAction: String?
    let symbol: String?
    let timestamp: Date?

    enum CodingKeys: String, CodingKey {
        case reasoning
        case lastAction = "last_action"
        case symbol, timestamp
    }
}
