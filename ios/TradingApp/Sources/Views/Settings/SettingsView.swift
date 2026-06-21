import SwiftUI

struct SettingsView: View {
    @AppStorage("appColorScheme") private var colorSchemePreference: String = "system"

    var body: some View {
        NavigationStack {
            List {
                // ── Appearance ────────────────────────────────────────────
                Section {
                    ForEach(["system", "light", "dark"], id: \.self) { option in
                        Button {
                            withAnimation(.easeInOut(duration: 0.2)) {
                                colorSchemePreference = option
                            }
                        } label: {
                            HStack(spacing: 14) {
                                ZStack {
                                    RoundedRectangle(cornerRadius: 8)
                                        .fill(iconBackground(option))
                                        .frame(width: 34, height: 34)
                                    Image(systemName: iconName(option))
                                        .font(.system(size: 16))
                                        .foregroundStyle(iconColor(option))
                                }
                                Text(optionLabel(option))
                                    .foregroundStyle(.primary)
                                Spacer()
                                if colorSchemePreference == option {
                                    Image(systemName: "checkmark")
                                        .font(.system(size: 14, weight: .semibold))
                                        .foregroundStyle(LakshmiTheme.gold)
                                }
                            }
                        }
                    }
                } header: {
                    Label("Appearance", systemImage: "paintbrush")
                        .font(.caption.weight(.semibold))
                }

                // ── About ─────────────────────────────────────────────────
                Section {
                    LabeledContent("App") { Text("Lakshmi").foregroundStyle(.secondary) }
                    LabeledContent("Version") { Text("1.0.0").foregroundStyle(.secondary) }
                    LabeledContent("AI Model") { Text("Claude Sonnet 4.6").foregroundStyle(.secondary) }
                } header: {
                    Label("About", systemImage: "info.circle")
                        .font(.caption.weight(.semibold))
                }
            }
            .navigationTitle("Settings")
            .tint(LakshmiTheme.gold)
        }
    }

    private func optionLabel(_ option: String) -> String {
        switch option {
        case "light":  return "Light"
        case "dark":   return "Dark"
        default:       return "System"
        }
    }

    private func iconName(_ option: String) -> String {
        switch option {
        case "light":  return "sun.max.fill"
        case "dark":   return "moon.fill"
        default:       return "iphone"
        }
    }

    private func iconBackground(_ option: String) -> Color {
        switch option {
        case "light":  return .yellow.opacity(0.15)
        case "dark":   return LakshmiTheme.purple.opacity(0.12)
        default:       return LakshmiTheme.blue.opacity(0.12)
        }
    }

    private func iconColor(_ option: String) -> Color {
        switch option {
        case "light":  return .yellow
        case "dark":   return LakshmiTheme.purple
        default:       return LakshmiTheme.blue
        }
    }
}
