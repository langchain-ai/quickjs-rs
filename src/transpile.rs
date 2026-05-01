//! TypeScript/TSX transpilation using OXC.
//!
//! This is used by module install to turn `.ts`/`.tsx` sources into
//! executable JavaScript before QuickJS parses modules.

use std::path::Path;

use oxc_allocator::Allocator;
use oxc_codegen::Codegen;
use oxc_parser::{ParseOptions, Parser};
use oxc_semantic::SemanticBuilder;
use oxc_span::SourceType;
use oxc_transformer::{TransformOptions, Transformer};

/// Transpile TypeScript-like source keys (`.ts`, `.mts`, `.cts`, `.tsx`)
/// into JavaScript. Non-TypeScript keys pass through unchanged.
pub(crate) fn maybe_transpile(key: &str, source: &str) -> Result<String, String> {
    let source_type = if key.ends_with(".ts") || key.ends_with(".mts") || key.ends_with(".cts") {
        SourceType::ts()
    } else if key.ends_with(".tsx") {
        SourceType::tsx()
    } else {
        // .js, .mjs, .cjs, no extension, anything else — pass through.
        return Ok(source.to_string());
    };

    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let allocator = Allocator::default();
        let parsed = Parser::new(&allocator, source, source_type)
            .with_options(ParseOptions {
                // Keep permissive parse behavior during install() so we don't
                // reject files on strictness unrelated to TS erasure.
                allow_return_outside_function: true,
                ..ParseOptions::default()
            })
            .parse();

        if parsed.panicked || !parsed.errors.is_empty() {
            let errors = parsed
                .errors
                .iter()
                .map(|d| d.to_string())
                .collect::<Vec<_>>();
            let msg = if errors.is_empty() {
                format!("TypeScript parse error in {}", key)
            } else {
                format!("TypeScript parse error in {}: {}", key, errors.join("; "))
            };
            return Err(msg);
        }

        let mut program = parsed.program;
        let trivias = parsed.trivias;
        let sem = SemanticBuilder::new(source)
            .with_trivias(trivias.clone())
            .build(&program);
        if !sem.errors.is_empty() {
            let errors = sem.errors.iter().map(|d| d.to_string()).collect::<Vec<_>>();
            return Err(format!(
                "TypeScript transform error in {}: {}",
                key,
                errors.join("; ")
            ));
        }

        let (symbols, scopes) = sem.semantic.into_symbol_table_and_scope_tree();
        let mut options = TransformOptions::default();
        // Preserve current resolver behavior: keep ".ts" literals in imports.
        options.typescript.rewrite_import_extensions = None;
        options.typescript.allow_namespaces = true;

        let transformed = Transformer::new(&allocator, Path::new(key), source, trivias, options)
            .build_with_symbols_and_scopes(symbols, scopes, &mut program);

        if !transformed.errors.is_empty() {
            let errors = transformed
                .errors
                .iter()
                .map(|d| d.to_string())
                .collect::<Vec<_>>();
            return Err(format!(
                "TypeScript transform error in {}: {}",
                key,
                errors.join("; ")
            ));
        }

        Ok(Codegen::new().build(&program).code)
    }));

    outcome.map_err(|_| format!("oxc panicked unexpectedly while parsing {}", key))?
}
