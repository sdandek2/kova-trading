import SwiftUI

// ─────────────────────────────────────────────────────────────────────────────
// KovaTheme — centralised design tokens for the Kova trading app.
// Supports light + dark mode automatically via adaptive colours.
// ─────────────────────────────────────────────────────────────────────────────

enum KovaTheme {

    // ── Brand gradient (matches app icon: pink → purple → blue) ──────────────
    static let pink   = Color(red: 1.00, green: 0.22, blue: 0.85)
    static let purple = Color(red: 0.61, green: 0.38, blue: 1.00)
    static let blue   = Color(red: 0.04, green: 0.52, blue: 1.00)
    static let cyan   = Color(red: 0.00, green: 0.85, blue: 1.00)

    static let brandGradient = LinearGradient(
        colors: [pink, purple, blue],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    static let blueGradient = LinearGradient(
        colors: [purple, blue, cyan],
        startPoint: .leading,
        endPoint: .trailing
    )

    // ── Subtle tint used for card accents ─────────────────────────────────────
    static let cardTint = LinearGradient(
        colors: [purple.opacity(0.10), blue.opacity(0.06)],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    // ── Semantic colours ──────────────────────────────────────────────────────
    static let accent:   Color = purple
    static let positive: Color = Color(red: 0.18, green: 0.80, blue: 0.44)  // emerald green
    static let negative: Color = Color(red: 1.00, green: 0.27, blue: 0.27)  // vivid red

    // ── Adaptive surface colours ──────────────────────────────────────────────
    /// Primary card background — grey6 in light, elevated in dark
    static var card: Color       { Color(.secondarySystemBackground) }
    /// Slightly more elevated surface
    static var cardElevated: Color { Color(.tertiarySystemBackground) }
    /// Page / scroll background
    static var pageBackground: Color { Color(.systemBackground) }

    // ── Corner radii ──────────────────────────────────────────────────────────
    static let radius:      CGFloat = 18
    static let radiusSm:    CGFloat = 12
    static let chipRadius:  CGFloat = 6

    // ── Spacing ───────────────────────────────────────────────────────────────
    static let pagePad:  CGFloat = 16
    static let cardPad:  CGFloat = 18

    // ── Typography helpers ────────────────────────────────────────────────────
    static func heroNumber(_ value: String) -> some View {
        Text(value)
            .font(.system(size: 40, weight: .bold, design: .rounded))
            .tracking(-0.5)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// View modifiers
// ─────────────────────────────────────────────────────────────────────────────

/// Standard Kova card surface
struct KovaCardModifier: ViewModifier {
    var padded: Bool = true
    func body(content: Content) -> some View {
        content
            .if(padded) { $0.padding(KovaTheme.cardPad) }
            .background(KovaTheme.card)
            .clipShape(RoundedRectangle(cornerRadius: KovaTheme.radius))
    }
}

/// Card with a subtle brand-gradient tint overlay
struct KovaAccentCardModifier: ViewModifier {
    var padded: Bool = true
    func body(content: Content) -> some View {
        content
            .if(padded) { $0.padding(KovaTheme.cardPad) }
            .background {
                ZStack {
                    KovaTheme.card
                    KovaTheme.cardTint
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: KovaTheme.radius))
    }
}

extension View {
    func kovaCard(padded: Bool = true) -> some View {
        modifier(KovaCardModifier(padded: padded))
    }
    func kovaAccentCard(padded: Bool = true) -> some View {
        modifier(KovaAccentCardModifier(padded: padded))
    }

    /// Conditional modifier helper
    @ViewBuilder
    func `if`<T: View>(_ condition: Bool, transform: (Self) -> T) -> some View {
        if condition { transform(self) } else { self }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Reusable chip / badge
// ─────────────────────────────────────────────────────────────────────────────

struct KovaChip: View {
    let text: String
    var color: Color = KovaTheme.accent
    var filled: Bool = true

    var body: some View {
        Text(text)
            .font(.caption2.weight(.bold))
            .foregroundStyle(filled ? .white : color)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(filled ? color : color.opacity(0.15))
            .clipShape(Capsule())
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// P&L badge
// ─────────────────────────────────────────────────────────────────────────────

struct PLBadge: View {
    let value: Double
    let percentValue: Double
    var showArrow: Bool = true

    private var isPositive: Bool { value >= 0 }
    private var color: Color { isPositive ? KovaTheme.positive : KovaTheme.negative }

    var body: some View {
        HStack(spacing: 4) {
            if showArrow {
                Image(systemName: isPositive ? "arrow.up.right" : "arrow.down.right")
                    .font(.caption.weight(.semibold))
            }
            Text(String(format: "%@$%.2f (%.2f%%)",
                        isPositive ? "+" : "", abs(value), abs(percentValue)))
                .font(.subheadline.weight(.semibold))
        }
        .foregroundStyle(color)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(color.opacity(0.12))
        .clipShape(Capsule())
    }
}
