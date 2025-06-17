//! Handles parsing of slide text properties and rendering of text overlays.
//!
//! This module defines how text styling and layout information is extracted from
//! a `Slide` model and then drawn onto the UI, including handling for scrolling text.

use egui::{FontId, Color32, Align, Painter, Rect, Vec2, Galley}; // Painter is not used, Galley is.
use crate::model::{Slide, FontAssets};
use crate::state_manager::TextScrollState;
use chrono::Local;
use log::{debug, warn, trace}; // Added trace

// --- Text Properties Structs ---

/// Defines the visual style of a piece of text to be rendered.
#[derive(Debug, Clone)]
pub struct TextStyle {
    pub font_id: FontId,
    pub color: Color32,
    pub background_color: Option<Color32>,
    /// Indicates if the text should scroll if it exceeds available width.
    pub scroll: bool,
}

/// Defines the layout and content of a piece of text.
#[derive(Debug, Clone)]
pub struct TextLayout {
    pub h_align: Align,
    pub v_align: Align,
    /// Padding around the text within its designated area.
    pub padding: f32,
    /// The actual text content to be displayed, after processing (e.g., `{datetime}` replacement).
    pub text_content: String,
}

// --- Parsing Function ---

/// Parses text-related properties from a `Slide` and `FontAssets` to produce
/// `TextStyle` and `TextLayout` structs ready for rendering.
///
/// This function handles defaults for missing properties and processes dynamic
/// content like `{datetime}`.
// #[must_use]` is not typically used for functions that primarily parse and return data structs unless failure is critical and returns Result.
pub fn parse_slide_text_properties(slide: &Slide, font_assets: &FontAssets) -> (TextStyle, TextLayout) {
    debug!("Parsing text properties for slide: '{}'", slide.name);

    let mut text_content = slide.text.as_ref().cloned().unwrap_or_default();
    if text_content.contains("{datetime}") {
        let formatted_datetime = Local::now().format("%Y-%m-%d %H:%M:%S").to_string();
        text_content = text_content.replace("{datetime}", &formatted_datetime);
        trace!("Replaced {{datetime}} with '{}' for slide: '{}'", formatted_datetime, slide.name);
    }

    let text_size_str = slide.text_size.as_ref().map_or("medium".to_string(), |s| s.clone());
    let font_id = font_assets.get_font_id(Some(&text_size_str));
    trace!("Slide '{}': text_size='{}', resolved font_id={:?}", slide.name, text_size_str, font_id);

    let color_hex = slide.text_color.as_ref().map_or("#FFFFFF", |s| s.as_str()); // Default to white
    let color = Color32::from_hex(color_hex).unwrap_or_else(|e| {
        warn!("Invalid text_color hex '{}' for slide '{}', defaulting to WHITE. Error: {:?}", color_hex, slide.name, e);
        Color32::WHITE
    });

    let background_color = slide.text_background_color.as_ref().and_then(|hex| {
        if hex.trim().is_empty() {
            None
        } else {
            Color32::from_hex(hex).map_err(|e| {
                warn!("Invalid text_background_color hex '{}' for slide '{}', defaulting to None. Error: {:?}", hex, slide.name, e);
            }).ok()
        }
    });

    let scroll = slide.scroll_text.unwrap_or(false);
    trace!("Slide '{}': scroll_text={}", slide.name, scroll);

    let style = TextStyle { font_id, color, background_color, scroll };

    let text_position_str = slide.text_position.as_ref().map_or("bottom-center", |s| s.as_str());
    trace!("Slide '{}': text_position='{}'", slide.name, text_position_str);
    let (h_align, v_align) = match text_position_str {
        "top-left" => (Align::Min, Align::Min),
        "top-center" => (Align::Center, Align::Min),
        "top-right" => (Align::Max, Align::Min),
        "center-left" => (Align::Min, Align::Center),
        "center" | "center-center" => (Align::Center, Align::Center),
        "center-right" => (Align::Max, Align::Center),
        "bottom-left" => (Align::Min, Align::Max),
        "bottom-center" => (Align::Center, Align::Max),
        "bottom-right" => (Align::Max, Align::Max),
        invalid => {
            warn!("Invalid text_position '{}' for slide '{}', defaulting to bottom-center.", invalid, slide.name);
            (Align::Center, Align::Max) // Default
        }
    };

    let layout = TextLayout {
        h_align,
        v_align,
        padding: 10.0, // Default padding for text within its box relative to media_rect
        text_content,
    };

    debug!("Parsed for slide '{}': Style={:?}, Layout={:?}", slide.name, style, layout);
    (style, layout)
}

/// Renders the text overlay onto the UI within the given `media_rect`.
///
/// Handles static and scrolling text, text alignment, color, background, and clipping.
/// Scroll progression itself is managed by `TextScrollState` and updated in `SlideshowApp::update`.
pub fn draw_text_overlay(
    ui: &mut egui::Ui,
    media_rect: Rect,
    slide_id_key: &str,
    style: &TextStyle,
    layout: &TextLayout,
    scroll_state_manager: &TextScrollState
) {
    if layout.text_content.trim().is_empty() { return; }

    let painter = ui.painter();
    // Prepare the text galley (layouted text)
    let galley = painter.layout_no_wrap(layout.text_content.clone(), style.font_id.clone(), style.color);

    // Determine the available width for the text box, considering padding
    let text_box_available_width = media_rect.width() - 2.0 * layout.padding;

    // Get current scroll offset if text needs to scroll
    let current_x_offset = if style.scroll && galley.size().x > text_box_available_width {
        scroll_state_manager.get_current_offset(slide_id_key, galley.size().x, text_box_available_width)
    } else {
        0.0 // No scroll needed or text fits
    };

    // --- Calculate Y position based on vertical alignment ---
    let mut text_pos_y = match layout.v_align {
        Align::Min => media_rect.top() + layout.padding,
        Align::Center => media_rect.center().y - galley.size().y / 2.0,
        Align::Max => media_rect.bottom() - galley.size().y - layout.padding,
    };
    // Ensure Y position is within media_rect bounds (considering galley height)
    text_pos_y = text_pos_y.max(media_rect.top()).min(media_rect.bottom() - galley.size().y);

    // --- Calculate initial X position based on horizontal alignment (before scrolling) ---
    let initial_text_pos_x = match layout.h_align {
        Align::Min => media_rect.left() + layout.padding,
        Align::Center => media_rect.center().x - galley.size().x / 2.0,
        Align::Max => media_rect.right() - galley.size().x - layout.padding,
    };

    // --- Determine final X position, applying scroll offset if needed ---
    let final_text_pos_x = if style.scroll && galley.size().x > text_box_available_width {
        // For scrolling text, its position is relative to the left edge of the padded media_rect,
        // offset by the current_x_offset.
        media_rect.left() + layout.padding + current_x_offset
    } else {
        // For static text, ensure it's within the padded bounds of media_rect.
        initial_text_pos_x.max(media_rect.left() + layout.padding)
                          .min(media_rect.right() - galley.size().x - layout.padding)
    };

    let final_text_pos = egui::pos2(final_text_pos_x, text_pos_y);

    // Define the clipping rectangle for text drawing (area where text is visible)
    let text_render_area = Rect::from_min_max(
        egui::pos2(media_rect.left() + layout.padding, text_pos_y - layout.padding / 2.0), // Allow some vertical padding for bg
        egui::pos2(media_rect.right() - layout.padding, text_pos_y + galley.size().y + layout.padding / 2.0)
    ).intersect(media_rect); // Ensure clip area is within media_rect

    // --- Background Drawing ---
    if let Some(bg_color) = style.background_color {
        let bg_width = if style.scroll && galley.size().x > text_box_available_width {
            // For scrolling text, background spans the entire clipped text_render_area width
            text_render_area.width()
        } else {
            // For static text, background fits the text galley width plus padding
            (galley.size().x + layout.padding).min(text_render_area.width())
        };
        // For static text, background X is relative to final_text_pos_x.
        // For scrolling, background X is fixed at the start of the text_render_area.
        let bg_pos_x = if style.scroll && galley.size().x > text_box_available_width {
            text_render_area.left()
        } else {
            // Ensure static background doesn't exceed render area if text is very wide but not scrolling
            final_text_pos.x - layout.padding / 2.0
        };

        let text_bg_rect = Rect::from_min_size(
            egui::pos2(bg_pos_x, final_text_pos.y - layout.padding / 2.0),
            Vec2::new(bg_width, galley.size().y + layout.padding)
        ).intersect(text_render_area); // Intersect with text_render_area to be safe

        painter.add(egui::Shape::Rect(egui::epaint::RectShape::filled(text_bg_rect, 5.0, bg_color))); // 5.0 for rounded corners
    }

    // --- Text Drawing (clipped) ---
    // Use a painter clipped to the text_render_area for drawing the galley.
    let text_painter = ui.painter_at(text_render_area);
    text_painter.galley(final_text_pos, galley, style.color);
}
