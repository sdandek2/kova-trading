import SwiftUI

// MARK: - Model
struct PreMarketScan: Decodable {
    let available: Bool
    let message: String?
    let scanned_at: String?
    let macro_regime: String?
    let top_stocks: [PreMarketStock]
    let headlines: [String]
}

struct PreMarketStock: Decodable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let mentions: Int
}

// MARK: - View
struct PreMarketView: View {
    @State private var scan: PreMarketScan? = nil
    @State private var isLoading = false
    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {

            // ── Header (always visible) ──
            Button {
                withAnimation(.spring(response: 0.3)) { isExpanded.toggle() }
            } label: {
                HStack(spacing: 10) {
                    ZStack {
                        Circle()
                            .fill(headerColor.opacity(0.15))
                            .frame(width: 36, height: 36)
                        Image(systemName: "sunrise.fill")
                            .foregroundStyle(headerColor)
                            .font(.system(size: 16))
                    }

                    VStack(alignment: .leading, spacing: 2) {
                        Text("Pre-Market Analysis")
                            .font(.headline)
                            .foregroundStyle(.primary)
                        Text(subtitleText)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }

                    Spacer()

                    if isLoading {
                        ProgressView().scaleEffect(0.8)
                    } else {
                        Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                            .foregroundStyle(.secondary)
                            .font(.caption)
                    }
                }
                .padding()
            }
            .buttonStyle(.plain)

            // ── Expanded content ──
            if isExpanded, let scan {
                Divider().padding(.horizontal)

                if !scan.available {
                    HStack(spacing: 8) {
                        Image(systemName: "clock")
                            .foregroundStyle(.secondary)
                        Text(scan.message ?? "Runs at 9:00–9:30 AM EST before market open.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding()
                } else {
                    VStack(alignment: .leading, spacing: 16) {

                        // Macro badge
                        if let regime = scan.macro_regime {
                            HStack(spacing: 6) {
                                Text("Regime")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                Text(regime.uppercased())
                                    .font(.caption)
                                    .fontWeight(.bold)
                                    .foregroundStyle(.white)
                                    .padding(.horizontal, 8)
                                    .padding(.vertical, 3)
                                    .background(regimeColor(regime))
                                    .clipShape(Capsule())

                                if let ts = scan.scanned_at {
                                    Spacer()
                                    Text(relativeTime(ts))
                                        .font(.caption2)
                                        .foregroundStyle(.tertiary)
                                }
                            }
                        }

                        // Top stocks
                        if !scan.top_stocks.isEmpty {
                            VStack(alignment: .leading, spacing: 6) {
                                Text("Top News Mentions")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .textCase(.uppercase)

                                LazyVGrid(columns: [
                                    GridItem(.flexible()),
                                    GridItem(.flexible()),
                                    GridItem(.flexible()),
                                ], spacing: 8) {
                                    ForEach(scan.top_stocks.prefix(9)) { stock in
                                        HStack(spacing: 4) {
                                            Text(stock.symbol)
                                                .font(.caption)
                                                .fontWeight(.semibold)
                                            Spacer()
                                            Text("\(stock.mentions)")
                                                .font(.caption2)
                                                .foregroundStyle(.secondary)
                                        }
                                        .padding(.horizontal, 10)
                                        .padding(.vertical, 6)
                                        .background(RoundedRectangle(cornerRadius: 8).fill(Color(.systemGray5)))
                                    }
                                }
                            }
                        }

                        // Headlines
                        if !scan.headlines.isEmpty {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("Overnight Headlines")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .textCase(.uppercase)

                                ForEach(scan.headlines.prefix(5), id: \.self) { headline in
                                    HStack(alignment: .top, spacing: 6) {
                                        Circle()
                                            .fill(Color.secondary.opacity(0.4))
                                            .frame(width: 5, height: 5)
                                            .padding(.top, 5)
                                        Text(cleanHeadline(headline))
                                            .font(.caption)
                                            .foregroundStyle(.primary)
                                            .lineLimit(2)
                                    }
                                }
                            }
                        }
                    }
                    .padding()
                }
            }
        }
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
        .task { await load() }
    }

    // MARK: - Helpers

    private var headerColor: Color {
        guard let scan, scan.available else { return .orange }
        return .green
    }

    private var subtitleText: String {
        guard let scan else { return "Loading..." }
        if !scan.available { return "Runs 9:00–9:30 AM EST" }
        let count = scan.top_stocks.count
        return "\(count) stocks on radar · tap to expand"
    }

    private func regimeColor(_ regime: String) -> Color {
        switch regime.lowercased() {
        case "bull": return .green
        case "bear": return .red
        default: return .orange
        }
    }

    private func cleanHeadline(_ raw: String) -> String {
        // Strip leading [source] [symbols] tags for cleaner display
        var h = raw
        while h.hasPrefix("["), let end = h.firstIndex(of: "]") {
            h = String(h[h.index(after: end)...]).trimmingCharacters(in: .whitespaces)
        }
        return h
    }

    private func relativeTime(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = formatter.date(from: iso) else { return "" }
        let mins = Int(-date.timeIntervalSinceNow / 60)
        if mins < 60 { return "\(mins)m ago" }
        return "\(mins / 60)h ago"
    }

    private func load() async {
        isLoading = true
        do {
            let response: PreMarketScan = try await APIService.shared.fetch("/api/trading/premarket")
            scan = response
        } catch {
            scan = PreMarketScan(available: false, message: "Could not load scan.", scanned_at: nil,
                                 macro_regime: nil, top_stocks: [], headlines: [])
        }
        isLoading = false
    }
}
