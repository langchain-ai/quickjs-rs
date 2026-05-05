//! Extract top-level declared names from JS source using oxc_parser.
//!
//! This is intentionally syntax-only extraction. It does not evaluate
//! code and does not attempt lexical-environment fidelity.

use oxc_allocator::Allocator;
use oxc_ast::ast::{BindingPattern, BindingPatternKind, Statement};
use oxc_parser::Parser;
use oxc_span::SourceType;

/// Parse source and extract top-level declared names in source order.
///
/// Returns `None` on parser error (caller should skip registry update).
pub(crate) fn extract_top_level_declared_names(source: &str, module: bool) -> Option<Vec<String>> {
    let source_type = if module { SourceType::mjs() } else { SourceType::cjs() };
    if let Some(names) = parse_declared_names(source, source_type) {
        return Some(names);
    }

    // Script-mode eval_async allows top-level await via JS_EVAL_FLAG_ASYNC.
    // The parser only accepts top-level await in module mode, so fall back
    // to a module parse solely for declaration extraction.
    if module {
        return None;
    }
    parse_declared_names(source, SourceType::mjs())
}

fn parse_declared_names(source: &str, source_type: SourceType) -> Option<Vec<String>> {
    let allocator = Allocator::default();
    let parsed = Parser::new(&allocator, source, source_type).parse();
    if parsed.panicked || !parsed.errors.is_empty() {
        return None;
    }

    let mut names = Vec::new();
    for stmt in &parsed.program.body {
        match stmt {
            Statement::VariableDeclaration(decl) => {
                for declarator in &decl.declarations {
                    collect_from_pattern(&declarator.id, &mut names);
                }
            }
            Statement::FunctionDeclaration(func) => {
                if let Some(id) = &func.id {
                    names.push(id.name.to_string());
                }
            }
            Statement::ClassDeclaration(class_decl) => {
                if let Some(id) = &class_decl.id {
                    names.push(id.name.to_string());
                }
            }
            _ => {}
        }
    }
    Some(names)
}

fn collect_from_pattern(pattern: &BindingPattern<'_>, out: &mut Vec<String>) {
    match &pattern.kind {
        BindingPatternKind::BindingIdentifier(id) => {
            out.push(id.name.to_string());
        }
        BindingPatternKind::AssignmentPattern(assign) => {
            collect_from_pattern(&assign.left, out);
        }
        BindingPatternKind::ObjectPattern(obj) => {
            for prop in &obj.properties {
                collect_from_pattern(&prop.value, out);
            }
            if let Some(rest) = &obj.rest {
                collect_from_pattern(&rest.argument, out);
            }
        }
        BindingPatternKind::ArrayPattern(arr) => {
            for elem in &arr.elements {
                if let Some(p) = elem {
                    collect_from_pattern(p, out);
                }
            }
            if let Some(rest) = &arr.rest {
                collect_from_pattern(&rest.argument, out);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::extract_top_level_declared_names;

    #[test]
    fn extracts_top_level_bindings() {
        let src = r#"
            const { a, b: c, ...rest } = obj;
            let [x, , y = 3, ...z] = arr;
            function f() {}
            class K {}
        "#;
        let got = extract_top_level_declared_names(src, false).unwrap();
        assert_eq!(got, vec!["a", "c", "rest", "x", "y", "z", "f", "K"]);
    }

    #[test]
    fn ignores_nested_declarations() {
        let src = r#"
            if (true) {
                const hidden = 1;
                function nope() {}
            }
            const top = 1;
        "#;
        let got = extract_top_level_declared_names(src, false).unwrap();
        assert_eq!(got, vec!["top"]);
    }

    #[test]
    fn parser_error_returns_none() {
        assert!(extract_top_level_declared_names("const =", false).is_none());
    }

    #[test]
    fn extracts_top_level_await_script_declarations() {
        let got = extract_top_level_declared_names(
            "await Promise.resolve('x'); const story = 'hi'",
            false,
        )
        .unwrap();
        assert_eq!(got, vec!["story"]);
    }

}
