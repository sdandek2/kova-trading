import SwiftUI

struct PositionRowView: View {
    let position: Position
    @Environment(\.colorScheme) private var colorScheme
    @GestureState private var pressing = false
    @State private var glowPulse = false

    private var isShort: Bool { position.side == "short" }
    private var plColor: Color { position.unrealizedPl >= 0 ? LakshmiTheme.positive : LakshmiTheme.negative }

    var body: some View {
        HStack(spacing: 12) {
            // ── Symbol icon ───────────────────────────────────────────────
            ZStack {
                RoundedRectangle(cornerRadius: 10)
                    .fill(isShort ? LakshmiTheme.negative.opacity(0.12) : LakshmiTheme.blue.opacity(0.12))
                    .frame(width: 42, height: 42)
                Text(String(position.symbol.prefix(2)))
                    .font(.system(size: 13, weight: .bold, design: .rounded))
                    .foregroundStyle(isShort ? LakshmiTheme.negative : LakshmiTheme.blue)
            }

            // ── Symbol + details ──────────────────────────────────────────
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(position.symbol)
                        .font(.subheadline.weight(.bold))
                    if isShort {
                        LakshmiChip(text: "SHORT", color: LakshmiTheme.negative)
                    }
                }
                Text(String(format: "%d shares · avg $%.2f",
                             Int(position.qty), position.avgEntryPrice))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            // ── Price + P&L ───────────────────────────────────────────────
            VStack(alignment: .trailing, spacing: 4) {
                Text(String(format: "$%.2f", position.currentPrice))
                    .font(.subheadline.weight(.semibold))
                    .contentTransition(.numericText())
                    .animation(.spring(response: 0.4, dampingFraction: 0.8), value: position.currentPrice)

                HStack(spacing: 3) {
                    Text(String(format: "%@$%.2f",
                                position.unrealizedPl >= 0 ? "+" : "",
                                position.unrealizedPl))
                    Text(String(format: "(%.1f%%)", position.unrealizedPlPercent))
                        .opacity(0.7)
                }
                .font(.caption.weight(.medium))
                .foregroundStyle(plColor)
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(plColor.opacity(0.10))
                .clipShape(Capsule())
            }
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 14)
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm))
        .overlay(
            RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm)
                .strokeBorder(
                    plColor.opacity(position.unrealizedPl != 0 ? 0.20 : 0),
                    lineWidth: 1
                )
        )
        .scaleEffect(pressing ? 0.97 : 1.0)
        .animation(.spring(response: 0.22, dampingFraction: 0.65), value: pressing)
        .shadow(
            color: plColor.opacity(position.unrealizedPl != 0 ? (glowPulse ? 0.28 : 0.06) : 0),
            radius: glowPulse ? 12 : 4, x: 0, y: 2
        )
        .animation(.easeInOut(duration: 2.2).repeatForever(autoreverses: true), value: glowPulse)
        .onAppear { if position.unrealizedPl != 0 { glowPulse = true } }
        .simultaneousGesture(
            DragGesture(minimumDistance: 0)
                .updating($pressing) { _, state, _ in state = true }
        )
    }
}
