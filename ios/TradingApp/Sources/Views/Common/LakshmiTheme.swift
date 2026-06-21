import SwiftUI

// ─────────────────────────────────────────────────────────────────────────────
// LakshmiTheme — centralised design tokens for the Kova trading app.
// Supports light + dark mode automatically via adaptive colours.
// ─────────────────────────────────────────────────────────────────────────────

enum LakshmiTheme {

    // ── Primary brand — Lakshmi gold (matches app icon) ──────────────────────
    static let gold    = Color(red: 1.00, green: 0.80, blue: 0.10)   // bright gold
    static let amber   = Color(red: 0.96, green: 0.52, blue: 0.05)   // warm amber
    static let saffron = Color(red: 1.00, green: 0.65, blue: 0.15)   // saffron accent

    static let brandGradient = LinearGradient(
        colors: [gold, amber],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    // Warm horizontal gradient (used in pickers, badges)
    static let warmGradient = LinearGradient(
        colors: [gold, saffron, amber],
        startPoint: .leading,
        endPoint: .trailing
    )

    // Legacy alias — kept so callers compile; now golden-warm
    static let blueGradient = LinearGradient(
        colors: [gold, amber],
        startPoint: .leading,
        endPoint: .trailing
    )

    // ── Secondary / semantic palette (unchanged — used for charts, signals) ───
    static let pink   = Color(red: 1.00, green: 0.22, blue: 0.85)
    static let purple = Color(red: 0.61, green: 0.38, blue: 1.00)
    static let blue   = Color(red: 0.04, green: 0.52, blue: 1.00)
    static let cyan   = Color(red: 0.00, green: 0.85, blue: 1.00)

    // ── Subtle tint used for card accents ─────────────────────────────────────
    static let cardTint = LinearGradient(
        colors: [gold.opacity(0.08), amber.opacity(0.05)],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )

    // ── Semantic colours ──────────────────────────────────────────────────────
    static let accent:   Color = gold
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
struct LakshmiCardModifier: ViewModifier {
    var padded: Bool = true
    func body(content: Content) -> some View {
        content
            .if(padded) { $0.padding(LakshmiTheme.cardPad) }
            .background(LakshmiTheme.card)
            .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radius))
    }
}

/// Card with a subtle brand-gradient tint overlay
struct LakshmiAccentCardModifier: ViewModifier {
    var padded: Bool = true
    func body(content: Content) -> some View {
        content
            .if(padded) { $0.padding(LakshmiTheme.cardPad) }
            .background {
                ZStack {
                    LakshmiTheme.card
                    LakshmiTheme.cardTint
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: LakshmiTheme.radius))
    }
}

extension View {
    func kovaCard(padded: Bool = true) -> some View {
        modifier(LakshmiCardModifier(padded: padded))
    }
    func kovaAccentCard(padded: Bool = true) -> some View {
        modifier(LakshmiAccentCardModifier(padded: padded))
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

struct LakshmiChip: View {
    let text: String
    var color: Color = LakshmiTheme.accent
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
// MARK: - Animation constants
// ─────────────────────────────────────────────────────────────────────────────

extension LakshmiTheme {
    static let springSnappy = Animation.spring(response: 0.30, dampingFraction: 0.72)
    static let springBouncy = Animation.spring(response: 0.48, dampingFraction: 0.62)
    static let springSmooth = Animation.spring(response: 0.45, dampingFraction: 0.88)
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Shimmer modifier  (skeleton-loading sweep)
// ─────────────────────────────────────────────────────────────────────────────

struct ShimmerModifier: ViewModifier {
    @State private var phase: CGFloat = -1.2
    func body(content: Content) -> some View {
        content
            .overlay(
                LinearGradient(
                    colors: [.white.opacity(0), .white.opacity(0.35), .white.opacity(0)],
                    startPoint: UnitPoint(x: phase, y: 0.5),
                    endPoint:   UnitPoint(x: phase + 0.6, y: 0.5)
                )
                .blendMode(.overlay)
                .allowsHitTesting(false)
            )
            .onAppear {
                withAnimation(.linear(duration: 1.6).repeatForever(autoreverses: false)) {
                    phase = 1.4
                }
            }
    }
}

extension View {
    func shimmer() -> some View { modifier(ShimmerModifier()) }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Card appear  (fade-up entrance, supports stagger delay)
// ─────────────────────────────────────────────────────────────────────────────

struct CardAppearModifier: ViewModifier {
    let delay: Double
    @State private var visible = false
    func body(content: Content) -> some View {
        content
            .opacity(visible ? 1 : 0)
            .offset(y: visible ? 0 : 18)
            .onAppear {
                withAnimation(.spring(response: 0.5, dampingFraction: 0.82).delay(delay)) {
                    visible = true
                }
            }
    }
}

extension View {
    func cardAppear(delay: Double = 0) -> some View { modifier(CardAppearModifier(delay: delay)) }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Press scale  (tactile feedback on tap)
// ─────────────────────────────────────────────────────────────────────────────

struct PressScaleButtonStyle: ButtonStyle {
    var scale: CGFloat = 0.96
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? scale : 1)
            .animation(.spring(response: 0.22, dampingFraction: 0.65), value: configuration.isPressed)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Live dot  (pulsing indicator for real-time data)
// ─────────────────────────────────────────────────────────────────────────────

struct LiveDot: View {
    var color: Color = LakshmiTheme.positive
    @State private var pulsing = false

    var body: some View {
        ZStack {
            Circle()
                .fill(color.opacity(0.3))
                .frame(width: 16, height: 16)
                .scaleEffect(pulsing ? 2.4 : 1)
                .opacity(pulsing ? 0 : 0.7)
                .animation(.easeOut(duration: 1.4).repeatForever(autoreverses: false), value: pulsing)
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
        }
        .onAppear { pulsing = true }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// MARK: - Glow border modifier
// ─────────────────────────────────────────────────────────────────────────────

struct GlowBorderModifier: ViewModifier {
    let color: Color
    let radius: CGFloat
    let cornerRadius: CGFloat

    func body(content: Content) -> some View {
        content
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius)
                    .stroke(color.opacity(0.55), lineWidth: 1)
            )
            .shadow(color: color.opacity(0.22), radius: radius, x: 0, y: 0)
    }
}

extension View {
    func glowBorder(color: Color = LakshmiTheme.gold, radius: CGFloat = 8,
                    cornerRadius: CGFloat = LakshmiTheme.radius) -> some View {
        modifier(GlowBorderModifier(color: color, radius: radius, cornerRadius: cornerRadius))
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// P&L badge
// ─────────────────────────────────────────────────────────────────────────────

struct PLBadge: View {
    let value: Double
    let percentValue: Double
    var showArrow: Bool = true
    @State private var didAppear = false

    private var isPositive: Bool { value >= 0 }
    private var color: Color { isPositive ? LakshmiTheme.positive : LakshmiTheme.negative }

    var body: some View {
        HStack(spacing: 4) {
            if showArrow {
                Image(systemName: isPositive ? "arrow.up.right" : "arrow.down.right")
                    .font(.caption.weight(.semibold))
            }
            Text(String(format: "%@$%.2f (%.2f%%)",
                        isPositive ? "+" : "", abs(value), abs(percentValue)))
                .font(.subheadline.weight(.semibold))
                .contentTransition(.numericText())
                .animation(.spring(response: 0.4, dampingFraction: 0.8), value: value)
        }
        .foregroundStyle(color)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(color.opacity(0.12))
        .clipShape(Capsule())
        .scaleEffect(didAppear ? 1 : 0.85)
        .opacity(didAppear ? 1 : 0)
        .animation(.spring(response: 0.4, dampingFraction: 0.7), value: didAppear)
        .onAppear { didAppear = true }
    }
}
