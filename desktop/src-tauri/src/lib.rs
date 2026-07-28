/// HotelOps AI — Tauri native shell.
///
/// This is a minimal native shell. HotelOps native functionality
/// will be added in later tasks through controlled Tauri commands.
/// This module follows least-privilege: no filesystem, shell, or
/// process access is exposed unless explicitly approved.

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
