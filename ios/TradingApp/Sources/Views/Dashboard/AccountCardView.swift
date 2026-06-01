import SwiftUI

struct AccountCardView: View {
    let account: AccountInfo
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        VStack(spacing: 0) {
            // ── Hero: portfolio value ─────────────────────────────────────
            VStack(spacing: 8) {
                Text("Portfolio Value")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)

                Text(formatCurrency(account.portfolioValue))
                    .font(.system(size: 42, weight: .bold, design: .rounded))
                    .tracking(-1)
                    .foregroundStyle(.primary)

                PLBadge(value: account.dayPl, percentValue: account.dayPlPercent)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 22)
            .padding(.bottom, 20)
            .background {
                // Subtle brand gradient tint at top
                ZStack(alignment: .top) {
                    KovaTheme.card
                    LinearGradient(
                        colors: [KovaTheme.purple.opacity(colorScheme == .dark ? 0.18 : 0.09),
                                 KovaTheme.blue.opacity(colorScheme == .dark ? 0.10 : 0.05),
                                 .clear],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                }
            }

            // ── Divider line with gradient ────────────────────────────────
            Rectangle()
                .fill(KovaTheme.purple.opacity(0.15))
                .frame(height: 1)

            // ── Cash + Buying Power ───────────────────────────────────────
            HStack(spacing: 0) {
                StatCell(label: "Tradeable Cash", value: formatCurrency(account.tradeableCash ?? account.cash), alignment: .leading)

                Rectangle()
                    .fill(Color(.separator))
                    .frame(width: 1, height: 36)

                StatCell(label: "Buying Power", value: formatCurrency(account.buyingPower), alignment: .trailing)
            }
            .padding(.vertical, 14)
            .padding(.horizontal, KovaTheme.cardPad)
            .background(KovaTheme.card)
        }
        .clipShape(RoundedRectangle(cornerRadius: KovaTheme.radius))
        .overlay(
            RoundedRectangle(cornerRadius: KovaTheme.radius)
                .strokeBorder(KovaTheme.purple.opacity(colorScheme == .dark ? 0.25 : 0.10), lineWidth: 1)
        )
        .shadow(color: KovaTheme.purple.opacity(colorScheme == .dark ? 0.0 : 0.08),
                radius: 12, x: 0, y: 4)
        .padding(.horizontal, KovaTheme.pagePad)
    }

    private func formatCurrency(_ value: Double) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .currency
        formatter.maximumFractionDigits = 2
        return formatter.string(from: NSNumber(value: value)) ?? "$\(value)"
    }
}

private struct StatCell: View {
    let label: String
    let value: String
    let alignment: HorizontalAlignment

    var body: some View {
        VStack(alignment: alignment, spacing: 3) {
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
        }
        .frame(maxWidth: .infinity, alignment: alignment == .leading ? .leading : .trailing)
    }
}
