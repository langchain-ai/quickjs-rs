//! OXC-backed source transform guest.
//!
//! ABI shape:
//! - Host writes `name` and `source` into linear memory with `qjst_alloc`.
//! - Host calls `qjst_transform(...)`.
//! - On OK, transformed source is available via `qjst_result_ptr/len`.
//! - On error, a diagnostic string is available via `qjst_error_ptr/len`.
//! - Host copies the active slot and calls `qjst_result_free`.

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;

use oxc_allocator::{Allocator, TakeIn, Vec as ArenaVec};
use oxc_ast::{ast::*, AstBuilder, NONE};
use oxc_codegen::{Codegen, CodegenOptions};
use oxc_parser::Parser;
use oxc_semantic::SemanticBuilder;
use oxc_span::{SourceType, Span};
use oxc_transformer::{TransformOptions, Transformer};

mod mem;

const STATUS_OK: i32 = 0;
const STATUS_UNCHANGED: i32 = 1;
const STATUS_BAD_INPUT: i32 = 2;
const STATUS_PARSE_ERROR: i32 = 3;
const STATUS_TRANSFORM_ERROR: i32 = 4;
const STATUS_PANIC: i32 = 5;

const FLAG_SOURCE_TS: u32 = 1 << 0;
const FLAG_SOURCE_TSX: u32 = 1 << 1;
const FLAG_STRIP_TYPESCRIPT: u32 = 1 << 8;
const FLAG_TOP_LEVEL_CONST_TO_VAR: u32 = 1 << 9;
const FLAG_STATIC_IMPORT_TO_DYNAMIC_IMPORT: u32 = 1 << 10;
const FLAG_SOURCE_MASK: u32 = FLAG_SOURCE_TS | FLAG_SOURCE_TSX;
const FLAG_PASS_MASK: u32 =
    FLAG_STRIP_TYPESCRIPT | FLAG_TOP_LEVEL_CONST_TO_VAR | FLAG_STATIC_IMPORT_TO_DYNAMIC_IMPORT;

#[no_mangle]
pub extern "C" fn qjst_transform(
    name_ptr: *const u8,
    name_len: usize,
    source_ptr: *const u8,
    source_len: usize,
    flags: u32,
) -> i32 {
    mem::qjst_result_free();
    let outcome = catch_unwind(AssertUnwindSafe(|| {
        let name = match mem::read_utf8(name_ptr, name_len) {
            Some(name) => name,
            None => return Err(TransformFailure::bad_input("invalid module name")),
        };
        let source = match mem::read_utf8(source_ptr, source_len) {
            Some(source) => source,
            None => return Err(TransformFailure::bad_input("invalid module source")),
        };
        transform_module_source(name, source, flags)
    }));

    match outcome {
        Ok(Ok(Some(code))) => {
            mem::set_result(code.into_bytes());
            STATUS_OK
        }
        Ok(Ok(None)) => STATUS_UNCHANGED,
        Ok(Err(err)) => {
            mem::set_error(err.message.into_bytes());
            err.status
        }
        Err(_) => {
            mem::set_error(b"transform guest panicked".to_vec());
            STATUS_PANIC
        }
    }
}

struct TransformFailure {
    status: i32,
    message: String,
}

impl TransformFailure {
    fn bad_input(message: impl Into<String>) -> Self {
        Self {
            status: STATUS_BAD_INPUT,
            message: message.into(),
        }
    }

    fn parse(message: impl Into<String>) -> Self {
        Self {
            status: STATUS_PARSE_ERROR,
            message: message.into(),
        }
    }

    fn transform(message: impl Into<String>) -> Self {
        Self {
            status: STATUS_TRANSFORM_ERROR,
            message: message.into(),
        }
    }
}

fn transform_module_source(
    name: &str,
    source: &str,
    flags: u32,
) -> Result<Option<String>, TransformFailure> {
    let Some(config) = TransformConfig::from_flags(flags)? else {
        return Ok(None);
    };

    let allocator = Allocator::default();
    let parser_return = Parser::new(&allocator, source, config.source_type).parse();
    if !parser_return.diagnostics.is_empty() {
        return Err(TransformFailure::parse(format_diagnostics(
            "source parse error",
            name,
            parser_return.diagnostics,
        )));
    }

    let mut program = parser_return.program;
    if config.strip_typescript {
        run_typescript_transform(&allocator, name, &mut program)?;
    }
    if config.static_import_to_dynamic_import {
        rewrite_static_imports_to_dynamic_import(&allocator, &mut program);
    }
    if config.top_level_const_to_var {
        rewrite_top_level_const_to_var(&mut program);
    }

    Ok(Some(codegen(&program)))
}

struct TransformConfig {
    source_type: SourceType,
    strip_typescript: bool,
    top_level_const_to_var: bool,
    static_import_to_dynamic_import: bool,
}

impl TransformConfig {
    fn from_flags(flags: u32) -> Result<Option<Self>, TransformFailure> {
        if flags & FLAG_PASS_MASK == 0 {
            return Ok(None);
        }

        let source_flags = flags & FLAG_SOURCE_MASK;
        if source_flags == FLAG_SOURCE_MASK {
            return Err(TransformFailure::bad_input(
                "source kind cannot be both TypeScript and TSX",
            ));
        }

        let strip_typescript = flags & FLAG_STRIP_TYPESCRIPT != 0;
        let top_level_const_to_var = flags & FLAG_TOP_LEVEL_CONST_TO_VAR != 0;
        let static_import_to_dynamic_import = flags & FLAG_STATIC_IMPORT_TO_DYNAMIC_IMPORT != 0;

        if strip_typescript && source_flags == 0 {
            return Err(TransformFailure::bad_input(
                "TypeScript stripping requires a TypeScript source kind",
            ));
        }

        let source_type = if source_flags == FLAG_SOURCE_TSX {
            SourceType::tsx().with_module(true)
        } else if source_flags == FLAG_SOURCE_TS {
            SourceType::ts().with_module(true)
        } else {
            SourceType::mjs()
        };

        Ok(Some(Self {
            source_type,
            strip_typescript,
            top_level_const_to_var,
            static_import_to_dynamic_import,
        }))
    }
}

fn run_typescript_transform<'a>(
    allocator: &'a Allocator,
    name: &str,
    program: &mut Program<'a>,
) -> Result<(), TransformFailure> {
    let semantic_return = SemanticBuilder::new()
        .with_excess_capacity(2.0)
        .with_enum_eval(true)
        .build(program);
    let scoping = semantic_return.semantic.into_scoping();
    let options = TransformOptions::default();
    let transform_return =
        Transformer::new(allocator, Path::new(name), &options).build_with_scoping(scoping, program);
    if !transform_return.diagnostics.is_empty() {
        return Err(TransformFailure::transform(format_diagnostics(
            "TypeScript transform error",
            name,
            transform_return.diagnostics,
        )));
    }
    Ok(())
}

fn rewrite_top_level_const_to_var(program: &mut Program<'_>) {
    for statement in &mut program.body {
        match statement {
            Statement::VariableDeclaration(declaration) => {
                rewrite_const_declaration_kind(&mut declaration.kind);
            }
            Statement::ExportNamedDeclaration(declaration) => {
                if let Some(Declaration::VariableDeclaration(variable)) =
                    declaration.declaration.as_mut()
                {
                    rewrite_const_declaration_kind(&mut variable.kind);
                }
            }
            _ => {}
        }
    }
}

fn rewrite_static_imports_to_dynamic_import<'a>(
    allocator: &'a Allocator,
    program: &mut Program<'a>,
) {
    let ast = AstBuilder::new(allocator);
    let mut rewritten_body = ast.vec_with_capacity(program.body.len());

    for statement in program.body.take_in(ast) {
        match statement {
            Statement::ImportDeclaration(import) => {
                let dynamic_specifier = import.source.value.as_str();
                append_dynamic_import_statements(
                    ast,
                    &mut rewritten_body,
                    &import,
                    dynamic_specifier,
                );
            }
            _ => rewritten_body.push(statement),
        }
    }

    program.body = rewritten_body;
}

fn append_dynamic_import_statements<'a>(
    ast: AstBuilder<'a>,
    out: &mut ArenaVec<'a, Statement<'a>>,
    import: &ImportDeclaration<'a>,
    dynamic_specifier: &str,
) {
    if import.import_kind.is_type() {
        return;
    }

    let Some(specifiers) = import.specifiers.as_ref() else {
        out.push(dynamic_import_expression_statement(
            ast,
            import.span,
            dynamic_specifier,
        ));
        return;
    };

    if specifiers.is_empty() {
        out.push(dynamic_import_expression_statement(
            ast,
            import.span,
            dynamic_specifier,
        ));
        return;
    }

    let mut namespace_local = None;
    let mut properties = ast.vec();
    for specifier in specifiers {
        match specifier {
            ImportDeclarationSpecifier::ImportDefaultSpecifier(default_specifier) => {
                properties.push(default_import_binding_property(ast, default_specifier));
            }
            ImportDeclarationSpecifier::ImportNamespaceSpecifier(namespace_specifier) => {
                namespace_local = Some(namespace_specifier.local.clone());
            }
            ImportDeclarationSpecifier::ImportSpecifier(import_specifier) => {
                if import_specifier.import_kind.is_value() {
                    properties.push(named_import_binding_property(ast, import_specifier));
                }
            }
        }
    }

    match namespace_local {
        Some(namespace_local) => {
            out.push(variable_declaration_statement(
                ast,
                import.span,
                binding_identifier_pattern(ast, &namespace_local),
                dynamic_import_expression(ast, import.span, dynamic_specifier),
            ));
            if !properties.is_empty() {
                out.push(variable_declaration_statement(
                    ast,
                    import.span,
                    ast.binding_pattern_object_pattern(import.span, properties, NONE),
                    ast.expression_identifier(namespace_local.span, namespace_local.name.clone()),
                ));
            }
        }
        None => {
            if properties.is_empty() {
                return;
            }
            out.push(variable_declaration_statement(
                ast,
                import.span,
                ast.binding_pattern_object_pattern(import.span, properties, NONE),
                dynamic_import_expression(ast, import.span, dynamic_specifier),
            ));
        }
    }
}

fn dynamic_import_expression_statement<'a>(
    ast: AstBuilder<'a>,
    span: Span,
    specifier: &str,
) -> Statement<'a> {
    ast.statement_expression(span, dynamic_import_expression(ast, span, specifier))
}

fn dynamic_import_expression<'a>(
    ast: AstBuilder<'a>,
    span: Span,
    specifier: &str,
) -> Expression<'a> {
    let source = ast.expression_string_literal(span, ast.str(specifier), None);
    let import_expression = ast.expression_import(span, source, None, None);
    ast.expression_await(span, import_expression)
}

fn variable_declaration_statement<'a>(
    ast: AstBuilder<'a>,
    span: Span,
    id: BindingPattern<'a>,
    init: Expression<'a>,
) -> Statement<'a> {
    let declaration = ast.variable_declarator(
        span,
        VariableDeclarationKind::Const,
        id,
        NONE,
        Some(init),
        false,
    );
    Statement::VariableDeclaration(ast.alloc_variable_declaration(
        span,
        VariableDeclarationKind::Const,
        ast.vec1(declaration),
        false,
    ))
}

fn default_import_binding_property<'a>(
    ast: AstBuilder<'a>,
    specifier: &ImportDefaultSpecifier<'a>,
) -> BindingProperty<'a> {
    ast.binding_property(
        specifier.span,
        ast.property_key_static_identifier(specifier.span, "default"),
        binding_identifier_pattern(ast, &specifier.local),
        false,
        false,
    )
}

fn named_import_binding_property<'a>(
    ast: AstBuilder<'a>,
    specifier: &ImportSpecifier<'a>,
) -> BindingProperty<'a> {
    ast.binding_property(
        specifier.span,
        module_export_name_to_property_key(ast, &specifier.imported),
        binding_identifier_pattern(ast, &specifier.local),
        false,
        false,
    )
}

fn module_export_name_to_property_key<'a>(
    ast: AstBuilder<'a>,
    name: &ModuleExportName<'a>,
) -> PropertyKey<'a> {
    match name {
        ModuleExportName::IdentifierName(identifier) => {
            ast.property_key_static_identifier(identifier.span, identifier.name.clone())
        }
        ModuleExportName::IdentifierReference(identifier) => {
            ast.property_key_static_identifier(identifier.span, identifier.name.clone())
        }
        ModuleExportName::StringLiteral(literal) => PropertyKey::StringLiteral(
            ast.alloc_string_literal(literal.span, literal.value.clone(), None),
        ),
    }
}

fn binding_identifier_pattern<'a>(
    ast: AstBuilder<'a>,
    local: &BindingIdentifier<'a>,
) -> BindingPattern<'a> {
    BindingPattern::BindingIdentifier(ast.alloc(local.clone()))
}

fn rewrite_const_declaration_kind(kind: &mut VariableDeclarationKind) {
    if *kind == VariableDeclarationKind::Const {
        *kind = VariableDeclarationKind::Var;
    }
}

fn codegen(program: &Program<'_>) -> String {
    Codegen::new()
        .with_options(CodegenOptions::default())
        .build(program)
        .code
}

fn format_diagnostics<T>(prefix: &str, name: &str, diagnostics: T) -> String
where
    T: IntoIterator,
    T::Item: std::fmt::Display,
{
    let messages: Vec<String> = diagnostics
        .into_iter()
        .map(|diagnostic| diagnostic.to_string())
        .collect();
    if messages.is_empty() {
        format!("{prefix} in {name}")
    } else {
        format!("{prefix} in {name}: {}", messages.join("; "))
    }
}
