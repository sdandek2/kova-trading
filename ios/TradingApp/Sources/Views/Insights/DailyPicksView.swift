import SwiftUI

struct DailyPicksView: View {
    @ObservedObject var vm: InsightsViewModel
    @State private var selectedSymbol: String? = nil
    @State private var showPrediction = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Section header
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Today's Growth Picks")
                        .font(.headline)
                    if let picks = vm.dailyPicks {
                        Text(formattedDate(picks.date))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                if vm.isLoadingDailyPicks {
                    ProgressView().scaleEffect(0.75)
                } else {
                    Button {
                        Task { await vm.loadDailyPicks(refresh: true) }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.horizontal)
            .padding(.bottom, 10)

            if let error = vm.dailyPicksError {
                Text(error)
                    .font(.caption).foregroundStyle(.red)
                    .padding(.horizontal)
            } else if vm.isLoadingDailyPicks && vm.dailyPicks == nil {
                HStack {
                    Spacer()
                    VStack(spacing: 10) {
                        ProgressView()
                        Text("Analyzing market for today's best picks…")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                }
                .padding(.vertical, 30)
            } else if let picks = vm.dailyPicks {
                // Market summary banner
                if !picks.summary.isEmpty {
                    Text(picks.summary)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal)
                        .padding(.bottom, 12)
                }

                // Regime + geo chips
                HStack(spacing: 8) {
                    regimeChip(picks.market_regime)
                    geoChip(picks.geo_risk_level)
                }
                .padding(.horizontal)
                .padding(.bottom, 14)

                // Short-term section
                if !picks.short_term.isEmpty {
                    picksSection(
                        title: "Short-Term Rockets",
                        emoji: "🔥",
                        subtitle: "1–2 weeks",
                        picks: picks.short_term,
                        accentColor: .orange
                    )
                }

                // Long-term section
                if !picks.long_term.isEmpty {
                    picksSection(
                        title: "Long-Term Compounders",
                        emoji: "🚀",
                        subtitle: "1–3 months",
                        picks: picks.long_term,
                        accentColor: .blue
                    )
                }

                // Avoid section
                if !picks.avoid_today.isEmpty {
                    avoidSection(picks.avoid_today)
                }
            } else {
                Button {
                    Task { await vm.loadDailyPicks() }
                } label: {
                    Label("Generate Today's Picks", systemImage: "sparkles")
                        .font(.subheadline.weight(.medium))
                }
                .frame(maxWidth: .infinity)
                .padding()
            }
        }
        .navigationDestination(isPresented: $showPrediction) {
            if let sym = selectedSymbol {
                PredictionDetailView(symbol: sym)
            }
        }
    }

    // MARK: - Picks section

    func picksSection(title: String, emoji: String, subtitle: String, picks: [DailyPick], accentColor: Color) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text(emoji)
                Text(title).font(.subheadline.weight(.semibold))
                Text("·").foregroundStyle(.secondary)
                Text(subtitle).font(.caption).foregroundStyle(.secondary)
            }
            .padding(.horizontal)

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 12) {
                    ForEach(picks) { pick in
                        DailyPickCard(pick: pick, accentColor: accentColor) {
                            selectedSymbol = pick.symbol
                            showPrediction = true
                        }
                    }
                }
                .padding(.horizontal)
                .padding(.bottom, 4)
            }
        }
        .padding(.bottom, 16)
    }

    // MARK: - Avoid section

    func avoidSection(_ items: [AvoidItem]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text("⚠️")
                Text("Avoid Today").font(.subheadline.weight(.semibold))
            }
            .padding(.horizontal)

            VStack(spacing: 6) {
                ForEach(items) { item in
                    HStack(alignment: .top, spacing: 8) {
                        Text(item.symbol_or_sector)
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.red)
                            .frame(minWidth: 60, alignment: .leading)
                        Text(item.reason)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal)
                }
            }
            .padding(.bottom, 14)
        }
    }

    // MARK: - Chips

    func regimeChip(_ regime: String) -> some View {
        let color: Color = regime == "bull" ? .green : regime == "bear" ? .red : .orange
        return Text(regime.uppercased())
            .font(.caption2.weight(.bold))
            .foregroundStyle(color)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .overlay(Capsule().stroke(color, lineWidth: 1))
    }

    func geoChip(_ level: String) -> some View {
        let color: Color = level == "low" ? .green : level == "moderate" ? .orange : level == "elevated" ? .orange : .red
        return Text("GEO: \(level.uppercased())")
            .font(.caption2.weight(.bold))
            .foregroundStyle(color)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .overlay(Capsule().stroke(color, lineWidth: 1))
    }

    func formattedDate(_ iso: String) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        if let d = formatter.date(from: iso) {
            formatter.dateStyle = .full
            formatter.timeStyle = .none
            return formatter.string(from: d)
        }
        return iso
    }
}

// MARK: - Individual pick card

struct DailyPickCard: View {
    let pick: DailyPick
    let accentColor: Color
    var onTap: () -> Void
    @State private var showTrade = false

    private var suggestedQty: Int {
        guard let price = pick.current_price_approx, price > 0 else { return 1 }
        let budget: Double = pick.confidence == "high" ? 1000 : 600
        return max(1, Int(budget / price))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Symbol + upside
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(pick.symbol)
                        .font(.title3.weight(.bold))
                    if let price = pick.current_price_approx {
                        Text(String(format: "~$%.2f", price))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    if let upside = pick.upside_pct {
                        Text(String(format: "+%.0f%%", upside))
                            .font(.title3.weight(.bold))
                            .foregroundStyle(accentColor)
                    }
                    if let target = pick.target_price {
                        Text(String(format: "→ $%.2f", target))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            // Type + confidence
            HStack(spacing: 6) {
                typeTag
                confidenceTag
            }

            // Thesis
            Text(pick.thesis)
                .font(.caption)
                .foregroundStyle(.primary)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)

            Divider()

            // Entry + risk
            VStack(alignment: .leading, spacing: 4) {
                Label(pick.entry_zone, systemImage: "arrow.down.circle")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Label(pick.key_risk, systemImage: "exclamationmark.triangle")
                    .font(.caption2)
                    .foregroundStyle(.orange)
                    .lineLimit(2)
            }

            // Buy button
            HStack {
                Text("AI: \(suggestedQty) share\(suggestedQty == 1 ? "" : "s")")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    showTrade = true
                } label: {
                    Text("Buy")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 6)
                        .background(Color.green)
                        .clipShape(Capsule())
                }
            }
        }
        .padding(14)
        .frame(width: 260)
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .shadow(color: accentColor.opacity(0.12), radius: 8, x: 0, y: 3)
        .overlay(
            RoundedRectangle(cornerRadius: 16)
                .stroke(accentColor.opacity(0.2), lineWidth: 1)
        )
        .contentShape(Rectangle())
        .onTapGesture { onTap() }
        .sheet(isPresented: $showTrade) {
            TradeSheet(prefillSymbol: pick.symbol, prefillSide: "buy", prefillQty: suggestedQty)
        }
    }

    var typeTag: some View {
        Text(pick.type.replacingOccurrences(of: "_", with: " "))
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .background(accentColor)
            .clipShape(Capsule())
    }

    var confidenceTag: some View {
        let color: Color = pick.confidence == "high" ? .green : .orange
        return Text(pick.confidence.uppercased())
            .font(.caption2.weight(.bold))
            .foregroundStyle(color)
            .padding(.horizontal, 7).padding(.vertical, 3)
            .overlay(Capsule().stroke(color, lineWidth: 1))
    }
}
