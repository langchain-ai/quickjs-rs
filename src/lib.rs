use pyo3::prelude::*;

#[pymodule]
fn _engine(_m: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
