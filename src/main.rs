use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;

#[derive(Parser)]
#[command(about = "Build cleaned and chunked Wikipedia datasets")]
struct Args {
    #[arg(long)]
    threads: Option<usize>,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Stage1 {
        xml: PathBuf,
        articles: PathBuf,
        #[arg(long)]
        metadata: PathBuf,
        #[arg(long, default_value_t = 250)]
        min_bytes: usize,
        #[arg(long)]
        limit: Option<usize>,
    },
    Stage2 {
        articles: PathBuf,
        chunks: PathBuf,
        #[arg(long, default_value_t = 700)]
        target_words: usize,
        #[arg(long, default_value_t = 128)]
        batch_size: usize,
    },
    Dataset {
        xml: PathBuf,
        articles: PathBuf,
        chunks: PathBuf,
        #[arg(long)]
        metadata: PathBuf,
        #[arg(long, default_value_t = 250)]
        min_bytes: usize,
        #[arg(long)]
        limit: Option<usize>,
        #[arg(long, default_value_t = 700)]
        target_words: usize,
    },
}

fn main() -> Result<()> {
    let args = Args::parse();
    if let Some(threads) = args.threads {
        rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .build_global()?;
    }
    match args.command {
        Command::Stage1 {
            xml,
            articles,
            metadata,
            min_bytes,
            limit,
        } => {
            println!(
                "{}",
                serde_json::to_string(&wiki2dataset::stage1(
                    xml, articles, metadata, min_bytes, limit
                )?)?
            )
        }
        Command::Stage2 {
            articles,
            chunks,
            target_words,
            batch_size,
        } => println!(
            "{}",
            wiki2dataset::stage2(articles, chunks, target_words, batch_size)?
        ),
        Command::Dataset {
            xml,
            articles,
            chunks,
            metadata,
            min_bytes,
            limit,
            target_words,
        } => {
            let result = wiki2dataset::dataset(
                xml,
                articles,
                chunks,
                metadata,
                min_bytes,
                limit,
                target_words,
            )?;
            println!("{}", serde_json::to_string(&result)?)
        }
    }
    Ok(())
}
