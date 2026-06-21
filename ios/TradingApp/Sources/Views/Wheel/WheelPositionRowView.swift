import SwiftUI

// ── Wheel Position Row ────────────────────────────────────────────────────────
// Shows a single active wheel position: phase badge, symbol, strike/expiry,
// premium decay progress bar (theta visualization), and total collected.

struct WheelPositionRowView: View {
    let position: WheelPosition
    @Environment(\.colorScheme) private var colorScheme
    @State private var barProgress: Double = 0

    private var phaseColor: Color {
        switch position.phase {
        case "put_open":         return LakshmiTheme.blue
        case "assigned":         return .orange
        case "call_open":        return LakshmiTheme.positive
        case "bear_spread_open": return LakshmiTheme.negative
        default:                 return .secondary
        }
    }

    // Theta decay: fraction of DTE consumed since position opened
    private var dteProgress: Double {
        guard let expiryStr = position.currentExpiry,
              let openedStr = position.openedAt else { return 0.5 }
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd"
        guard let expiry = df.date(from: String(expiryStr.prefix(10))) else { return 0.5 }
        let isoDF = ISO8601DateFormatter()
        isoDF.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let opened = isoDF.date(from: openedStr) ?? Date()
        let totalDays = expiry.timeIntervalSince(opened) / 86400
        let elapsed = Date().timeIntervalSince(opened) / 86400
        guard totalDays > 0 else { return 1.0 }
        return min(max(elapsed / totalDays, 0), 1.0)
    }

    private var dteRemaining: String {
        guard let expiryStr = position.currentExpiry else { return "—" }
        let df = DateFormatter()
        df.dateFormat = "yyyy-MM-dd"
        guard let expiry = df.date(from: String(expiryStr.prefix(10))) else { return "—" }
        let days = Calendar.current.dateComponents([.day], from: Date(), to: expiry).day ?? 0
        return "\(max(days, 0))d"
    }

    var body: some View {
        HStack(spacing: 12) {
            // ── Symbol icon ───────────────────────────────────────────────
            ZStack {
                RoundedRectangle(cornerRadius: 10)
                    .fill(phaseColor.opacity(0.14))
                    .frame(width: 44, height: 44)
                Text(String(position.symbol.prefix(2)))
                    .font(.system(size: 13, weight: .bold, design: .rounded))
                    .foregroundStyle(phaseColor)
            }

            // ── Main content ──────────────────────────────────────────────
            VStack(alignment: .leading, spacing: 4) {
                // Symbol + phase badge
                HStack(spacing: 6) {
                    Text(position.symbol)
                        .font(.subheadline.weight(.bold))
                    LakshmiChip(text: position.phaseLabel, color: phaseColor)
                    if position.contractsRemaining < 2 {
                        LakshmiChip(text: "1 left", color: .orange)
                    }
                }

                // Strike / expiry / DTE
                if let strike = position.putStrike ?? position.callStrike {
                    HStack(spacing: 4) {
                        Text("$\(strike, specifier: "%.2f") strike")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text("·")
                            .foregroundStyle(.tertiary)
                        Text(dteRemaining + " left")
                            .font(.caption)
                            .foregroundStyle(dteProgress > 0.7 ? LakshmiTheme.positive : .secondary)
                    }
                }

                // Theta decay bar (animates from 0 → actual on appear)
                if position.phase == "put_open" || position.phase == "call_open" {
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            RoundedRectangle(cornerRadius: 3)
                                .fill(Color.secondary.opacity(0.2))
                                .frame(height: 4)
                            RoundedRectangle(cornerRadius: 3)
                                .fill(
                                    LinearGradient(
                                        colors: [LakshmiTheme.blue.opacity(0.6), LakshmiTheme.positive],
                                        startPoint: .leading, endPoint: .trailing
                                    )
                                )
                                .frame(width: geo.size.width * barProgress, height: 4)
                                .animation(.spring(response: 0.9, dampingFraction: 0.8).delay(0.2), value: barProgress)
                        }
                    }
                    .frame(height: 4)
                    .onAppear {
                        withAnimation { barProgress = dteProgress }
                    }
                }
            }

            Spacer()

            // ── P&L / premium collected ───────────────────────────────────
            VStack(alignment: .trailing, spacing: 3) {
                if let total = position.totalPremiumCollected, total > 0 {
                    Text(String(format: "+$%.0f", total))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(LakshmiTheme.positive)
                    Text("collected")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                } else if let regime = position.regimeAtOpen {
                    Text(regime.capitalized)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 6)
    }
}
