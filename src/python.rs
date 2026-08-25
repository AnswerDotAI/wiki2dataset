use crate::{
    BuildSummary, TemplateRoles, cache_page_props, convert_article, convert_wikitext, dataset,
    stage1, stage2, title_prefix,
};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedStr;
use rayon::prelude::*;
use std::path::PathBuf;

fn error(error: anyhow::Error) -> PyErr {
    PyRuntimeError::new_err(format!("{error:#}"))
}

type PyBuildSummary = (usize, usize, Vec<(String, String, usize)>);

fn summary(result: BuildSummary) -> PyBuildSummary {
    (
        result.articles,
        result.chunks,
        result
            .remaining_operations
            .into_iter()
            .map(|x| (x.op, x.name, x.pages))
            .collect(),
    )
}

#[pyclass(module = "wiki2dataset")]
struct WikiConverter {
    templates: TemplateRoles,
}

#[pymethods]
impl WikiConverter {
    #[new]
    fn new(metadata: PathBuf) -> PyResult<Self> {
        Ok(Self {
            templates: TemplateRoles::open(metadata).map_err(error)?,
        })
    }

    fn wiki2md(&self, py: Python<'_>, source: &str) -> (Option<String>, Vec<String>) {
        py.detach(|| match convert_wikitext(source, &self.templates) {
            Some(converted) => (Some(converted.md), converted.warnings),
            None => (None, Vec::new()),
        })
    }

    fn wiki2md_batch(
        &self,
        py: Python<'_>,
        sources: Vec<PyBackedStr>,
    ) -> Vec<(Option<String>, Vec<String>)> {
        py.detach(move || {
            sources
                .par_iter()
                .map(|source| match convert_wikitext(source, &self.templates) {
                    Some(converted) => (Some(converted.md), converted.warnings),
                    None => (None, Vec::new()),
                })
                .collect()
        })
    }

    #[pyo3(signature = (pages, target_words=700))]
    fn wiki2dataset_batch(
        &self,
        py: Python<'_>,
        pages: Vec<(PyBackedStr, PyBackedStr)>,
        target_words: usize,
    ) -> Vec<(Option<String>, Vec<String>, Vec<(String, String)>)> {
        py.detach(move || {
            pages
                .par_iter()
                .map(|(title, source)| {
                    match convert_article(source, &self.templates, target_words) {
                        Some((converted, chunks)) => {
                            let title_md = title_prefix(title);
                            let chunks = chunks
                                .into_iter()
                                .enumerate()
                                .map(|(i, chunk)| {
                                    let md = format!(
                                        "{title_md}{}{}",
                                        chunk.prefix, &converted.md[chunk.range]
                                    );
                                    (
                                        md,
                                        if i == 0 {
                                            "h1".into()
                                        } else {
                                            chunk.start.as_str()
                                        },
                                    )
                                })
                                .collect();
                            (Some(converted.md), converted.warnings, chunks)
                        }
                        None => (None, Vec::new(), Vec::new()),
                    }
                })
                .collect()
        })
    }
}

#[pyfunction]
#[pyo3(signature = (xml, articles, metadata, min_bytes=250, limit=None))]
fn build_stage1(
    py: Python<'_>,
    xml: PathBuf,
    articles: PathBuf,
    metadata: PathBuf,
    min_bytes: usize,
    limit: Option<usize>,
) -> PyResult<PyBuildSummary> {
    py.detach(|| stage1(xml, articles, metadata, min_bytes, limit))
        .map(summary)
        .map_err(error)
}

#[pyfunction]
#[pyo3(signature = (articles, chunks, target_words=700, batch_size=128))]
fn build_stage2(
    py: Python<'_>,
    articles: PathBuf,
    chunks: PathBuf,
    target_words: usize,
    batch_size: usize,
) -> PyResult<usize> {
    py.detach(|| stage2(articles, chunks, target_words, batch_size))
        .map_err(error)
}

#[pyfunction]
#[pyo3(signature = (xml, articles, chunks, metadata, min_bytes=250, limit=None, target_words=700))]
fn build_dataset(
    py: Python<'_>,
    xml: PathBuf,
    articles: PathBuf,
    chunks: PathBuf,
    metadata: PathBuf,
    min_bytes: usize,
    limit: Option<usize>,
    target_words: usize,
) -> PyResult<PyBuildSummary> {
    py.detach(|| {
        dataset(
            xml,
            articles,
            chunks,
            metadata,
            min_bytes,
            limit,
            target_words,
        )
    })
    .map(summary)
    .map_err(error)
}

#[pyfunction]
fn build_page_props(py: Python<'_>, source: PathBuf, metadata: PathBuf) -> PyResult<usize> {
    py.detach(|| cache_page_props(source, metadata))
        .map_err(error)
}

#[pymodule]
fn _core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<WikiConverter>()?;
    module.add_function(wrap_pyfunction!(build_stage1, module)?)?;
    module.add_function(wrap_pyfunction!(build_stage2, module)?)?;
    module.add_function(wrap_pyfunction!(build_dataset, module)?)?;
    module.add_function(wrap_pyfunction!(build_page_props, module)?)?;
    Ok(())
}
