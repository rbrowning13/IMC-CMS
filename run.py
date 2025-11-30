from app import create_app

app = create_app()
app.config["LOAD_TEST_DATA"] = True

if __name__ == "__main__":
    app.run(debug=True)
