import SwiftUI

struct PerformanceView: View {
    @State private var stats: PerformanceStats?
    @State private var isLoading = false
    @State private var errorMessage: String?

    var body: some View {
        Group {
            if isLoading && stats == nil {
                LoadingView()
            } else if let error = errorMessage, stats == nil {
                ErrorView(message: error) { await load() }
            } else if let stats {
                ScrollView {
                    VStack(spacing: 16) {
                        // Returns comparison
                        VStack(alignment: .leading, spacing: 12) {
                            Text("Returns (1 Month)")
                                .font(.headline)
                            HStack(spacing: 16) {
                                StatCard(
                                    title: "Your Bot",
                                    value: stats.portfolioReturn1m.map { String(format: "%@%.2f%%", $0 >= 0 ? "+" : "", $0) } ?? "N/A",
                                    color: (stats.portfolioReturn1m ?? 0) >= 0 ? .green : .red
                                )
                                StatCard(
                                    title: "SPY",
                                    value: stats.spyReturn1m.map { String(format: "%@%.2f%%", $0 >= 0 ? "+" : "", $0) } ?? "N/A",
                                    color: (stats.spyReturn1m ?? 0) >= 0 ? .green : .red
                                )
                                StatCard(
                                    title: "Alpha",
                                    value: stats.alpha.map { String(format: "%@%.2f%%", $0 >= 0 ? "+" : "", $0) } ?? "N/A",
                                    color: (stats.alpha ?? 0) >= 0 ? .green : .red
                                )
                            }
                        }
                        .padding()
                        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))

                        // Trading stats
                        VStack(alignment: .leading, spacing: 12) {
                            Text("Trading Stats")
                                .font(.headline)
                            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                                StatCard(title: "Total Trades", value: "\(stats.totalTrades)", color: .primary)
                                StatCard(title: "Win Rate", value: String(format: "%.1f%%", stats.winRate), color: stats.winRate >= 50 ? .green : .red)
                                StatCard(title: "Avg Win", value: String(format: "$%.2f", stats.avgWin), color: .green)
                                StatCard(title: "Avg Loss", value: String(format: "$%.2f", stats.avgLoss), color: .red)
                                StatCard(title: "Profit Factor", value: String(format: "%.2fx", stats.profitFactor), color: stats.profitFactor >= 1 ? .green : .red)
                                StatCard(title: "Sharpe Ratio", value: String(format: "%.2f", stats.sharpeRatio), color: stats.sharpeRatio >= 1 ? .green : .orange)
                            }
                        }
                        .padding()
                        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
                    }
                    .padding()
                }
                .refreshable { await load() }
            }
        }
        .task { await load() }
    }

    private func load() async {
        isLoading = true
        errorMessage = nil
        do {
            stats = try await APIService.shared.getPerformance()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}

struct StatCard: View {
    let title: String
    let value: String
    var color: Color = .primary

    var body: some View {
        VStack(spacing: 4) {
            Text(value)
                .font(.title3)
                .fontWeight(.bold)
                .foregroundStyle(color)
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding()
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }
}
