import Foundation

struct ModelTierSettings: Codable {
    let primary: String
    let fallback: String
    let primary_options: [String]
    let fallback_options: [String]
}

struct ModelSettings: Codable {
    let pro: ModelTierSettings
    let standard: ModelTierSettings
}

struct ModelSettingsUpdate: Codable {
    var pro_primary: String?
    var pro_fallback: String?
    var standard_primary: String?
    var standard_fallback: String?
}
