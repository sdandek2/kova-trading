import SwiftUI

struct PredictionDetailView: View {
    let symbol: String
    @StateObject private var vm = InsightsViewModel()

    var body: some View {
        ScrollView {
            if vm.isLoadingPrediction {
                VStack(spacing: 16) {
                    ProgressView()
                    Text("Analyzing \(symbol)...")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 80)
            } else if let error = vm.predictionError {
                VStack(spacing: 12) {
                    Image(systemName: "exclamationmark.triangle")
                        .font(.largeTitle)
                        .foregroundStyle(.orange)
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity)
                .padding(.top, 80)
            } else if let p = vm.prediction {
                PredictionContent(
                    prediction: p,
                    livePrice: vm.livePrices[symbol]
                )
            }
        }
        .navigationTitle(symbol)
        .navigationBarTitleDisplayMode(.large)
        .task {
            await vm.loadPrediction(symbol: symbol)
        }
    }
}

// MARK: - Full prediction layout

struct PredictionContent: View {
    let prediction: StockPrediction
    var livePrice: Double? = nil   // real-time override from Alpaca

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            // Header: price + recommendation
            headerCard

            // Price targets
            targetsCard

            // Signal alignment
            signalsCard

            // Scenarios
            scenariosCard

            // Catalysts
            listCard(title: "Key Catalysts", icon: "bolt.fill", color: .green, items: prediction.key_catalysts)

            // Risks
            listCard(title: "Key Risks", icon: "exclamationmark.triangle.fill", color: .red, items: prediction.key_risks)

            // AI reasoning
            reasoningCard

            if let expires = prediction.cache_expires_at {
                Text("Analysis refreshes at \(expires.prefix(16).replacingOccurrences(of: "T", with: " ")) UTC")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding(.bottom, 8)
            }
        }
        .padding()
    }

    // MARK: Header

    var headerCard: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 6) {
                Text(prediction.symbol)
                    .font(.largeTitle.weight(.bold))
                // Show live price (green dot) if available, else fall back to cached
                if let price = livePrice ?? prediction.current_price {
                    HStack(spacing: 6) {
                        Text(String(format: "$%.2f", price))
                            .font(.title2.weight(.medium))
                            .foregroundStyle(.primary)
                        if livePrice != nil {
                            HStack(spacing: 3) {
                                LiveDot(color: LakshmiTheme.positive)
                                    .scaleEffect(0.55)
                                    .frame(width: 10, height: 10)
                                Text("LIVE").font(.caption2.weight(.bold)).foregroundStyle(LakshmiTheme.positive)
                            }
                        }
                    }
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 6) {
                recommendationBadge
                Text("\(prediction.confidence.uppercased()) CONFIDENCE")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.07), radius: 8, x: 0, y: 2)
    }

    var recommendationBadge: some View {
        Text(prediction.recommendation.replacingOccurrences(of: "_", with: " ").uppercased())
            .font(.caption.weight(.bold))
            .foregroundStyle(.white)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(recommendationColor)
            .clipShape(Capsule())
    }

    var recommendationColor: Color {
        switch prediction.recommendation {
        case "strong_buy": return LakshmiTheme.positive
        case "buy":        return LakshmiTheme.positive.opacity(0.75)
        case "sell":       return LakshmiTheme.negative.opacity(0.75)
        case "strong_sell":return LakshmiTheme.negative
        default:           return LakshmiTheme.amber
        }
    }

    // MARK: Price Targets

    var targetsCard: some View {
        cardContainer(title: "Price Targets", icon: "target") {
            VStack(spacing: 12) {
                targetRow(label: "1 Week", target: prediction.targets.week_1)
                Divider()
                targetRow(label: "1 Month", target: prediction.targets.month_1)
                Divider()
                targetRow(label: "3 Months", target: prediction.targets.month_3)
            }
        }
    }

    func targetRow(label: String, target: PriceTarget) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text(label)
                .font(.subheadline.weight(.medium))
                .frame(width: 72, alignment: .leading)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 10) {
                    if let price = target.price {
                        Text(String(format: "$%.2f", price))
                            .font(.subheadline.weight(.semibold))
                    }
                    if let chg = target.change_pct {
                        Text(String(format: "%@%.1f%%", chg >= 0 ? "+" : "", chg))
                            .font(.caption.weight(.medium))
                            .foregroundStyle(chg >= 0 ? .green : .red)
                    }
                }
                Text(target.rationale)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: Signals

    var signalsCard: some View {
        cardContainer(title: "Signal Alignment", icon: "waveform.path.ecg") {
            HStack(spacing: 0) {
                signalItem(label: "Technical", value: prediction.technical_signal)
                Divider().frame(height: 40)
                signalItem(label: "Sentiment", value: prediction.sentiment_signal)
                Divider().frame(height: 40)
                signalItem(label: "Macro", value: prediction.macro_alignment)
            }
        }
    }

    func signalItem(label: String, value: String) -> some View {
        VStack(spacing: 4) {
            Text(signalEmoji(value))
                .font(.title2)
            Text(value.capitalized)
                .font(.caption.weight(.medium))
                .foregroundStyle(signalColor(value))
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }

    func signalEmoji(_ s: String) -> String {
        switch s {
        case "bullish", "aligned": return "🟢"
        case "bearish", "against": return "🔴"
        default: return "🟡"
        }
    }

    func signalColor(_ s: String) -> Color {
        switch s {
        case "bullish", "aligned": return .green
        case "bearish", "against": return .red
        default: return .orange
        }
    }

    // MARK: Scenarios

    var scenariosCard: some View {
        cardContainer(title: "Scenarios", icon: "chart.line.uptrend.xyaxis") {
            VStack(spacing: 12) {
                scenarioRow(label: "Bull Case", color: .green, scenario: prediction.scenarios.bull)
                Divider()
                scenarioRow(label: "Base Case", color: .blue, scenario: prediction.scenarios.base)
                Divider()
                scenarioRow(label: "Bear Case", color: .red, scenario: prediction.scenarios.bear)
            }
        }
    }

    func scenarioRow(label: String, color: Color, scenario: Scenario) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Circle().fill(color).frame(width: 8, height: 8).padding(.top, 5)
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(label).font(.subheadline.weight(.semibold))
                    Spacer()
                    if let target = scenario.price_target {
                        Text(String(format: "$%.2f", target))
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(color)
                    }
                    Text("(\(scenario.probability))")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Text(scenario.trigger)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: List card (catalysts / risks)

    func listCard(title: String, icon: String, color: Color, items: [String]) -> some View {
        cardContainer(title: title, icon: icon, iconColor: color) {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: icon)
                            .font(.caption2)
                            .foregroundStyle(color)
                            .padding(.top, 3)
                        Text(item)
                            .font(.caption)
                    }
                }
            }
        }
    }

    // MARK: Reasoning

    var reasoningCard: some View {
        cardContainer(title: "AI Investment Thesis", icon: "brain") {
            Text(prediction.reasoning)
                .font(.subheadline)
                .foregroundStyle(.primary)
                .lineSpacing(4)
        }
    }

    // MARK: Generic card container

    func cardContainer<Content: View>(title: String, icon: String, iconColor: Color = .blue, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(iconColor)
                Text(title)
                    .font(.subheadline.weight(.semibold))
            }
            content()
        }
        .padding(16)
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 6, x: 0, y: 2)
    }
}
