import SwiftUI

struct OrderRowView: View {
    let order: Order
    @State private var appeared = false

    private var isBuy:    Bool   { order.side == "buy" }
    private var sideColor: Color { isBuy ? LakshmiTheme.positive : LakshmiTheme.negative }
    private var sideLabel: String { order.side.uppercased() }
    private var isActive: Bool {
        ["accepted", "pending_new", "new", "partially_filled"].contains(order.status)
    }

    var body: some View {
        HStack(spacing: 12) {
            // ── Side icon ─────────────────────────────────────────────────
            ZStack {
                RoundedRectangle(cornerRadius: 10)
                    .fill(sideColor.opacity(0.12))
                    .frame(width: 42, height: 42)
                Image(systemName: isBuy ? "arrow.down.circle.fill" : "arrow.up.circle.fill")
                    .foregroundStyle(sideColor)
                    .font(.system(size: 18))
            }

            // ── Symbol + qty ──────────────────────────────────────────────
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(order.symbol)
                        .font(.subheadline.weight(.bold))
                    LakshmiChip(text: sideLabel, color: sideColor)
                }
                Text("\(Int(order.qty)) shares")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            // ── Price + status ────────────────────────────────────────────
            VStack(alignment: .trailing, spacing: 3) {
                if let price = order.filledAvgPrice {
                    Text(String(format: "$%.2f", price))
                        .font(.subheadline.weight(.semibold))
                }
                Text(order.status.replacingOccurrences(of: "_", with: " ").capitalized)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(statusColor(order.status))
                Text(order.createdAt, style: .relative)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 14)
        .background(LakshmiTheme.card)
        .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm))
        .overlay(
            RoundedRectangle(cornerRadius: LakshmiTheme.radiusSm)
                .stroke(sideColor.opacity(isActive ? 0.22 : 0.08), lineWidth: 1)
        )
        .shadow(color: sideColor.opacity(isActive ? 0.08 : 0), radius: 6, x: 0, y: 2)
        .opacity(isActive ? 1.0 : 0.52)
        .scaleEffect(appeared ? 1 : 0.95)
        .animation(.spring(response: 0.45, dampingFraction: 0.82), value: appeared)
        .onAppear { appeared = true }
    }

    private func statusColor(_ status: String) -> Color {
        switch status {
        case "filled":                          return LakshmiTheme.positive
        case "canceled", "rejected", "expired": return LakshmiTheme.negative
        default:                                return .secondary
        }
    }
}
