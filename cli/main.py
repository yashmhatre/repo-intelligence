import typer

app = typer.Typer()

@app.command()
def health():
    print("Repo Intelligence CLI is working")

if __name__ == "__main__":
    app()